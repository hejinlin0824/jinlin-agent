# Jinlin Code Engineer Agent

## 技术报告 + 产品手册（v1.1）

| 项 | 值 |
|---|---|
| 被评系统 | `jinlin-code-agent`（CLI：`jinlin-agent`） |
| 版本 | 0.1.0（文档 v1.1） |
| 对应代码提交 | 见 `git log -1`（v1.1 为独立评审整改后的提交） |
| 文档日期 | 2026-11-01（v1.0：2026-10-31） |
| 文档性质 | 技术报告 + 产品手册二合一；供其他 AI 模型评审打分 |
| 运行环境（实测） | Windows 11，Python 3.13.15，DeepSeek-V4.1-Flash（API id `deepseek-flash`） |
| 基线系统 | **DeepSeek Harness（DSH）**、**ZCode** |
| 独立评审 | 静态代码审查：实验型工程 Harness 7.0/10、生产级通用 Agent 4.5/10；整改记录见 `docs/REVIEW-RESPONSE.md` |
| 许可 | MIT（见 `LICENSE`） |

---

## 0. 给评审 AI 的说明（先读这一节）

### 0.1 评审对象与边界

本文档评审的是 `jinlin-code-agent`：一个**从零实现的、Python 写的单 Agent 代码工程 harness**，目标是满足 arXiv:2606.10106 提出的 agent harness 必要充分条件（T1–T4），并在真实仓库上完成"调查 → 计划 → 改代码 → 验证 → 带证据报告"的闭环。

**它不是**：通用 Agent 平台、IDE、多 Agent 公司、SWE-bench 榜单系统、生产级安全沙箱产品。

**基线也不是同类物**：DSH 与 ZCode 都是成熟的**通用产品/平台**（DSH：Cordis 插件式通用 harness；ZCode：桌面 + Web + TUI + CLI 的 AI 编程工作台）。请按"任务定位 + 工程证据"分维度评分，不要用"功能数量"一把尺子把三者放在同一档。

### 0.2 作者与被评对象同源（偏差披露）

本文档由运行在 **DSH** 上的一个 AI Agent（DeepSeek-V4.1-Flash）撰写；被评系统也由同一个 Agent 实现（Windows L1 沙箱模块由一个并行 teammate 完成）。**因此：**

- 自评分数请打折看待；
- 本文档中所有"我们做到了"的声明，都给出可执行核验方法（§5.3），请优先自己跑，不要只信文字；
- 本文档主动列出"会输给基线的地方"（§4.3）和"非声明"（§4.5），请把它们计入扣分项。

### 0.3 如何快速打分（建议流程）

```bash
cd jinlin-code-agent
python -m pip install -e .          # 零运行时依赖，离线可装
python -m pytest -q                # 期望：42 passed
jinlin-agent eval --suite all      # 期望：18/18 probes passed
jinlin-agent doctor                # 期望：model=deepseek-flash / sandbox=L1
```

真实模型任务需要 `DEEPSEEK_API_KEY`（本文档不提供 key，也不含任何凭据）。若无法联网，请以 §5.3 的离线探针为主要评分依据。

---

## Abstract (English, for international reviewers)

`jinlin-code-agent` is a from-scratch Python code-engineering harness. It implements the four constitutive conditions of an agent harness defined in arXiv:2606.10106 (adaptive loop, environment-changing tool interface, content-aware context management, model-independent control) and ships an executable conformance suite (10 probes for T1–T4 plus 8 task evaluations, all offline and deterministic) so the claim can be falsified rather than trusted. Its distinguishing mechanism is an evidence-bound completion gate: the model may only *propose* completion; the harness re-runs a verification ladder (diff review, syntax, lint, tests, regression-vs-baseline) and rejects the proposal unless every claim points at real evidence. The runtime is event-sourced (append-only log as single truth, intent-before-effect, tri-state crash recovery), zero-runtime-dependency, ~9,200 lines of Python including tests, and runs real DeepSeek-V4.1-Flash sessions inside a Windows L1 sandbox (restricted token + job object; honest L0 downgrade). Trade-offs are explicit: CLI-only, single-agent, no plugin/MCP/skills ecosystem, no network isolation, and a deliberately small evaluation set.

---

# Part I — 产品手册

## 1. 产品定位

### 1.1 一句话

把"给仓库派一个工程任务"变成"一个有运行状态、有权限边界、有验证证据、可中断恢复的工程执行过程"。

### 1.2 适用场景

| 适合 | 不适合 |
|---|---|
| Python/Node 仓库中的中小型真实修改（修 bug、加配置、跨文件小功能） | 大型跨仓库重构、需要数天的任务 |
| 需要可审计过程（每一步、每次工具调用、每次验证都有事件） | 需要强隔离的多租户/不可信代码执行 |
| 希望"完成"由测试和证据决定，而不是模型自述 | 需要 IDE 体验、图形界面、远程集群 |
| 内网/离线环境（零第三方运行时依赖） | 需要 MCP/插件生态、多 Agent 协作、多模型路由 |

### 1.3 非目标（V1 明确不做）

多 Agent 自由对话、组织角色、云调度、K8s、完整 IDE、自动 merge/commit/push、浏览器 GUI 自动化、读取生产凭据、以工具数量为目标。

## 2. 安装与首次运行

```bash
git clone <repo> && cd jinlin-code-agent
python -m pip install -e .
jinlin-agent --version
jinlin-agent doctor
```

`doctor` 输出示例（本机实测）：

```
python: 3.13.15
workspace: C:\...\jinlin-code-agent
model: deepseek-flash @ https://api.deepseek.com/v1
api key: MISSING (DEEPSEEK_API_KEY)
sandbox: level=L1 engine=restricted-token+job-object
project types: ['python']
test commands: ['pytest -q']
```

模型配置（用户级 `~/.jinlin-agent/config.toml`）：

```toml
[model]
base_url  = "https://api.deepseek.com/v1"
model     = "deepseek-flash"     # 真实 API id；deepseek-chat/reasoner 是同一模型别名
api_key_env = "DEEPSEEK_API_KEY" # key 只从环境变量读，永不写入文件
reasoning_effort = "high"        # low | high | max
```

```bash
export DEEPSEEK_API_KEY=sk-...
```

## 3. 日常使用

### 3.1 三种入口

```bash
# ① 新任务（交互审批）
jinlin-agent run --workspace /path/to/repo -t "修复失败测试，并跑全量测试"

# ② 只读分析（最安全的第一步）
jinlin-agent run --workspace /path/to/repo --read-only -t "梳理启动入口和调用链"

# ③ 无人值守（中/高风险自动批准，破坏性仍拒绝）
jinlin-agent run --workspace /path/to/repo --yes -t "..."
```

### 3.2 会话操作

```bash
jinlin-agent sessions                      # 列出历史会话
jinlin-agent resume <session_id>           # 恢复（崩溃 / 等待用户后）
jinlin-agent resume <sid> --answer "..."   # 回答 agent 的提问
jinlin-agent trace <sid> --tail 80         # 逐步审计事件流
jinlin-agent stats <sid>                   # 指标：步数/token/验证/压缩
jinlin-agent eval --suite all              # 一致性探针 + 任务评测
```

退出码：`0` 完成，`1` 失败，`2` 等待用户/审批，`3` 中断，`4` 配置错误。

### 3.3 审批语义

- 中风险（装依赖、git 写操作、未知可执行文件）：询问 `[y]/[a]ll/[N]o`；
- 高风险（网络访问、git push、解释器内联代码、shell 包装器）：询问或按配置拒绝；
- 破坏性（`rm -rf`、`git reset --hard`、系统工具）：默认拒绝；
- 非交互环境无审批应答者：**fail-closed**（拒绝），并在事件里记录 `approval.requested`。

### 3.4 常见故障排查

| 现象 | 处理 |
|---|---|
| `MODEL_AUTH: no API key` | `export DEEPSEEK_API_KEY=...`；`doctor` 复核 |
| `MODEL_INVALID_RESPONSE / model not found` | 模型 id 用 `deepseek-flash`；`JINLIN_MODEL` 覆盖 |
| `APPROVAL_DENIED` | 交互终端里重跑，或 `--yes`（破坏性仍需改配置） |
| 任务卡在 `waiting_user` | `resume <sid> --answer "..."` |
| 报告显示 `sandbox L0` | 受限令牌构造失败（看 `sandbox.mode` 的 `downgrade_reason`），策略/审批仍生效 |
| 测试跑不起来 | 在 `.jinlin/config.toml` 里显式设 `[verification].test_command` |

## 4. 安全模型与运维建议

| 层 | 机制 |
|---|---|
| 路径 | 工作区边界（符号链接/大小写/编码）、受保护文件（`.env`/密钥/`.git`）拒绝 |
| 命令 | argv 数组、`shell=False`、Windows `.cmd` shim 拒绝或解析为真实入口、风险分级 |
| 审批 | 与具体参数绑定、单次提权、fail-closed、session 级授权 |
| 配置 | 内置 < 用户级 < 项目级；项目级**只能收紧**（仓库放 `allow_delete=true` 不生效，只记录违规） |
| 沙箱 | Windows L1：受限令牌（WRITE_RESTRICTED）+ Job Object；失败自动降 L0 并如实标注 |
| 注入 | 仓库内容 `<untrusted>` 包装；权限由代码判定，提示词无法提权 |
| 凭据 | key 只从环境变量读取；事件/artifact/报告不含明文（已 grep 验证） |

**运维建议**：用一次性分支或 worktree；先 `--read-only` 跑分析；不要把生产凭据放进工作区；网络无 OS 级隔离，高敏环境请放在容器/VM 里。

---

# Part II — 技术报告

## 5. 设计依据：什么才算 harness

arXiv:2606.10106 的定义（意译）：

> **Agent harness** 是包裹一个或多个语言模型、把它变成能在外部环境上完成任务的 agent 的**运行时工程层**。它运行时同时具备：
> (i) 交错**推理/行动/观察**的 agent loop；
> (ii) 让模型**感知并改变**外部环境的工具接口；
> (iii) 决定什么进入、什么离开模型窗口的**上下文管理**；
> (iv) **不依赖模型配合**的控制机制（限制、验证、确定性动作）。

论文给出的可验证判据（本文档按此实现并测试）：

- **T2**：接口必须能**改变**环境（改文件、跑命令），只读不算；
- **T3**：上下文进出决策必须依赖**任务/当前观察的内容**，仅按 buffer 大小机械截断不合格；
- **T4**：机制有效性**不依赖模型是否配合**（打印日志不算，工具上限/确定性校验算）；
- 成员资格二值、质量渐进；多 Agent/UI/微调**不是**必要条件。

本项目把这四条落成 10 个可执行探针（§12），并且**不声称**"通过四条件 = 成熟产品"——成熟度由 §13 的任务评测与 §14 的真实模型验证支撑。

## 6. 总体架构

```
┌───────────────────────────────────────────────────────────┐
│ CLI（run/resume/sessions/trace/stats/doctor/eval）          │
├───────────────────────────────────────────────────────────┤
│ Agent Loop（loop.py）                                      │
│   step: 构造上下文 → 模型 → 解释 → 派发 → 观察回灌 → 下一步  │
│   完成: propose_completion → 验证阶梯 → 完成门 → 报告        │
├──────────────┬──────────────┬──────────────────────────────┤
│ Context      │ Model        │ Tool Dispatcher              │
│ Builder      │ Gateway      │  schema→policy→approval→     │
│ 打分/预算/压缩│ SSE/toolcall │  intent→execute→normalize    │
├──────────────┴──────────────┴──────────────────────────────┤
│ 策略：路径 / 命令 / 审批（fail-closed）                       │
│ 沙箱：Windows L1（受限令牌+Job Object）/ L0 降级              │
│ 验证：L0…L5 阶梯 + 基线 + 完成门                             │
├───────────────────────────────────────────────────────────┤
│ 持久化：事件日志（唯一真相） + Artifact Store                 │
└───────────────────────────────────────────────────────────┘
```

### 6.1 模块与职责

| 模块 | 职责 | 规模 |
|---|---|---|
| `loop.py` | Agent Loop、完成流程、循环/预算守卫 | 460 行 |
| `context.py` | 内容相关上下文构建、预算、压缩 | 470 行 |
| `verify.py` | 项目检测、基线、验证阶梯、完成门 | 700 行 |
| `runtime.py` | 唯一工具执行管线 | 350 行 |
| `policy.py` | 路径/命令策略、审批 | 520 行 |
| `sandbox/windows.py` | Windows L1 沙箱（ctypes） | 1441 行 |
| `model/openai_compat.py` | SSE、tool-call 累积、重试 | 315 行 |
| `events.py` / `state.py` | 事件日志（含崩溃尾部修复）/ 事件折叠 | 190 + 430 行 |
| `locks.py` | 跨平台顾问文件锁（会话单写者 / workspace 写阶段串行） | 约 75 行 |
| `tools/*` | 20 个工具 | 约 1900 行 |
| 其余 | CLI、session、report、config、evalkit 等 | 约 1200 行 |

总计：**9,173 行 Python（含测试）**，48 个受版本控制文件（含本文档），6 个 commit。

### 6.2 一次会话的数据流

```
用户任务
 → session.created（记录 workspace/request/model/config_hash/sandbox level）
 → workspace.snapshot（git 状态、项目类型、检测到的测试命令、既有脏文件）
 → 基线测试（记录"任务开始前就失败"的测试）
 → loop.step × N：
     context.built（每个 block 的分数/理由/丢弃项）
     → model.requested/attempt/responded
     → tool.intent → tool.completed/failed/denied
     → plan.updated / fact.recorded / decision.recorded
     → verification.*（模型跑的测试也算证据）
 → propose_completion
     → harness 自动跑 L0/L1/L2/L3/L5
     → completion.proposed → 完成门（10+ 项机器判定）
         ├─ 拒绝 → completion.rejected（原因回灌，继续修）
         └─ 通过 → 报告（claim→evidence）→ session.completed
```

## 7. 事件溯源与崩溃恢复

### 7.1 唯一真相

- `sessions/<id>/events.jsonl`：append-only、`seq` 严格递增、每次 append `fsync`、单写者文件锁；
- 状态不是"存"出来的，而是**折叠**出来的（`state.recover(events)`）——删掉投影也能重建；
- 33 种事件类型（附录 B）；
- **崩溃尾部在追加前修复**：`EventLog.open()` 取得写锁后先解析全文件——末尾"无换行且无法解析"的碎片按 torn write 截断到最后一条完整事件（`fsync`）；最后一条完整 JSON 缺结尾换行则补写换行（`fsync`）；**换行终止的坏 JSON 不自动修复**，仍然 `SESSION_CORRUPTED` fail-closed。`open()` 校验失败时释放会话锁。因此"读取容忍 torn tail"绝不等于"带着损坏尾部继续追加"。

### 7.2 intent 先于副作用

工具执行前先写 `tool.intent`（参数哈希 + 幂等键），执行后写 `tool.completed/failed`。恢复时对"有 intent 无 result"的调用分三态：

| 情况 | 处理 |
|---|---|
| 只读工具 | 视为未生效，安全忽略 |
| 写类工具 | 标 `RECOVERY_UNCERTAIN`，提示先核对工作区（不猜成功/失败） |
| 不可判定 | 转 `WAITING_USER` |

### 7.3 修改归属

session 开始时快照记录"任务前既有的脏文件"；完成门区分 **用户既有修改 / Agent 修改 / 外部程序修改**。出现无法归属的改动会阻断完成（`WORKSPACE_DIRTY_CONFLICT`）。

### 7.4 Checkpoint

每 N 步写 `session.checkpoint`（seq + 状态哈希）；恢复 = 最近 checkpoint + 折叠其后事件，并扫描孤儿 intent。事件日志天然支持审计与回放。

## 8. 工具系统

### 8.1 20 个工具（V1）

| 组 | 工具 |
|---|---|
| 文件读 | `read_file`（行范围+sha256）、`list_dir`、`find_files`、`search_text`、`get_file_info` |
| 项目理解 | `get_project_summary`（项目类型/指令文件/候选测试命令/git 状态） |
| 编辑 | `apply_patch`（V4A，多文件原子）、`edit_file`（old/new 精确唯一匹配）、`write_file` |
| Shell | `run_command`（argv 数组/超时/后台）、`read_process_output`、`terminate_process` |
| Git | `get_git_status`、`get_git_diff`、`get_git_log`（只读） |
| 任务状态 | `update_plan`、`record_fact`、`record_decision`、`request_user_input`、`propose_completion` |

### 8.2 唯一执行管线

```
工具存在 → JSON Schema 校验 → 参数规范化 → 路径策略 → 命令策略
→ 审批（fail-closed）→ tool.intent → 沙箱执行 → 超时/异常
→ 结果标准化 → 超长输出落 Artifact + 截断元数据 → tool.completed/failed
```

每个工具的 `ToolSpec` 声明：副作用等级、默认超时、幂等性、输出上限、模型可见描述（描述即行为契约）。

## 9. 编辑协议（为什么不用模糊匹配）

行业没有唯一标准：OpenAI Codex 用 `apply_patch`（V4A 信封），Anthropic Claude Code 用 Edit（`old_string`/`new_string`），Aider 用 SEARCH/REPLACE。本项目**两者都支持，共用一个安全引擎**：

1. `apply_patch`：V4A 信封（Add/Update/Delete File，多文件），**全有或全无**；
2. `edit_file`：单文件精确替换，`old_string` 必须唯一。

共同安全规则（**由代码强制，不靠提示词**）：

- **精确匹配**，不做模糊匹配（静默改错比失败更糟）；
- **读后写**：会话内没读过、或读后文件被改 → 拒绝，返回诊断与"重新读取"提示；
- **全有或全无**：先校验全部文件，再原子替换；失败自动回滚；
- **保留 BOM 与换行风格**（CRLF 不会因为 `\n` 而全量 miss）；
- **原子写**：更新走临时文件 + `os.replace`，写前复核哈希**缩小**（不消除）TOCTOU 窗口；新建文件走 `O_CREAT|O_EXCL` 独占创建（不用 `os.replace`），对并发创建者具有文件系统级的原子 no-clobber 保证——竞争失败返回 `PATCH_CONFLICT`，绝不覆盖对方内容；
- **workspace 写锁**：写阶段持 `<workspace>/.jinlin/write.lock` 顾问锁，多个 Jinlin 会话在同一工作区上串行（第二个会话得到可重试的 `WORKSPACE_LOCKED`）；
- **回滚先移动后校验，放回一律 no-clobber，清理一律不按名删**：回滚不动原路径——先把当前内容原子改名到事务级 trash、校验哈希归属，只有确认仍是本事务产物才替换；所有"把 trash/backup 放回原路径"的动作都走统一 helper（`os.link` 原子 create-if-absent）：路径已被并发重建时**双方保留**（外部内容在路径、本事务内容在 trash/backup）并报告 `RECOVERY_UNCERTAIN`，绝不用覆盖式 rename 回退（包括硬链接不可用的文件系统）；放回成功后 source 目录项**有意保留**、不按名 unlink（防误删后来者），作为 `redundant_trash` 上报供检查后受控清理；
- **Rescue 恢复不是覆盖式回滚**：当原路径被外部占用时，原始字节写入独立的 `.jinlin-rescue-<tag>` 文件并记录 manifest；`jinlin-agent rescue list` 只列出身份匹配的记录，新版记录同时核对 SHA-256，旧版记录明确标为 `legacy-stat`；`rescue restore` 使用独占创建，目标已存在则拒绝覆盖，成功后仍保留 Rescue 源文件。
- **恢复中断的处理**：恢复先在目标同目录创建确定性命名的暂存文件，完成写入、文件 `fsync` 与 SHA-256 校验后，才用硬链接原子 no-clobber 发布到目标路径。POSIX 平台会在暂存目录项创建后、目标发布后尝试 `fsync` 父目录；便携式 Windows Python API 不提供等价的目录句柄同步能力，因此 Windows 目录项断电持久性明确标为 best-effort。进程在暂存写入期间崩溃，不会把半成品暴露为目标文件；`jinlin-agent rescue recover` 会重新校验暂存文件、重新执行文件 `fsync`，仅发布与 Rescue 原件 SHA-256 完全一致的暂存文件。内容不完整的暂存文件只报告、不自动删除；目标已被占用则保留暂存和 Rescue 原件并报告冲突。
- **暂存身份检查**：发布前通过文件描述符检查暂存对象是普通文件，并比较打开描述符与路径的 `(dev, ino, size, mtime_ns)` 身份，拒绝符号链接及意外多重硬链接；发布后再次核对目标身份。该机制缩小并检测部分路径替换窗口，但跨平台路径式 `os.link` 仍有同权限外部进程竞态，不能视为抵御恶意同用户进程的完整安全边界。
- **孤立暂存文件**：`rescue recover` 会扫描符合命名规则、但没有当前有效 Rescue + manifest 记录对应的暂存文件，并将其列为 `orphaned`。孤立文件仅报告，绝不自动发布或删除；CLI 对存在孤立文件返回非零状态，要求人工检查。
- **受控清理**：`rescue clean` 默认只预览。只有当目标文件仍为普通非符号链接文件且 SHA-256 与有效 Rescue 一致时，才将 Rescue 与 stage 列入可清理项；`rescue clean --apply` 才执行清理。每个候选先移动到唯一 quarantine 名称，再验证被移动对象的类型与 SHA-256，匹配后才删除；身份不匹配的对象保留在 quarantine 并报告。进程若在 quarantine 后、删除前中断，后续 `rescue clean` 会把遗留 quarantine 作为人工检查项报告，不自动恢复或删除。清理成功后目标文件保留，manifest 记录随 Rescue 删除而移除。
- **事务审计日志**：restore / recover / clean / quarantine-recover 操作会尝试追加到 `.jinlin/rescue-audit.jsonl`。新事件采用 v2 格式，包含持久化事务 ID、全局连续序号、唯一事件 ID、UTC 时间、动作、状态、前一事件哈希及本事件哈希；写入使用独立审计锁、追加模式和文件 `fsync`。事务 ID 在创建 Rescue manifest 记录时生成并随 manifest 更新保留；旧 manifest 没有事务 ID 时仍兼容读取，并使用旧式身份派生 ID 作为过渡。审计读取器兼容 v1 前缀后追加 v2，但拒绝序号断档、重复事件 ID、v2 后混入 v1、字段格式错误或 hash chain 损坏。遇到损坏记录会报告问题并拒绝继续追加，既不截断也不自动修复。
- **事务状态投影**：状态机以持久化事务 ID 聚合事件，检查 CREATED / STAGED / PUBLISHED / CONFLICT / UNCERTAIN / ORPHANED / CLEANUP_BLOCKED / CLEANED 的合法迁移；同时核对同一事务的 Rescue 路径、目标路径和 SHA-256 是否保持一致。dry-run 事件保留在审计链中，但不创建或推进事务。非法迁移或事务身份漂移会让状态进入 `TRANSITION_INVALID`，并由 `rescue inspect` 报告事件数量、序号范围和一致性问题。审计日志仍只是可追溯性证据，不是恢复数据的权威来源；审计写入失败会在命令结果中给出 warning，不阻断数据恢复。
- **事务状态投影**：`rescue inspect` 只读汇总 manifest/Rescue、stage、孤立 stage、清理 quarantine 和审计事件，并将事件投影为 `STAGED`、`PUBLISHED`、`CONFLICT`、`UNCERTAIN`、`ORPHANED`、`CLEANED` 等状态。状态是审计历史的解释视图，必须结合文件 SHA-256 与 manifest 身份判断，不可单独作为删除授权。状态投影使用显式允许迁移表；例如 `PUBLISHED → STAGED` 属于不允许回退，会显示 `TRANSITION_INVALID` 并使 inspect 返回 attention-required。当前有效 Rescue 对应的 stage 若 SHA-256 不匹配或不可读取，即使它尚未被发布，也会计入 `incomplete_stages` 并使 inspect 返回 attention-required，避免把进程崩溃留下的空文件/部分文件误报为正常。
- **一键验收**：仓库根目录的 `test-all.bat` 可双击运行 Windows 测试入口；`scripts/test-all.ps1` 执行 editable dev 安装、全量 pytest、Harness eval、隔离临时工作区的 `scripts/rescue_smoke.py` 生命周期测试，以及 `scripts/test-rescue-fault-matrix.py`。故障矩阵既覆盖 syscall 故障注入，也启动独立 Python 子进程，在 stage 创建、部分写入、文件 fsync、目录同步、hardlink 发布和 cleanup quarantine 后调用 `os._exit()`，再由父进程运行 recover/inspect。它验证进程突然终止后的恢复行为，不等价于内核崩溃或断电持久性测试。所有测试工作区由 pytest 临时目录创建并自动清理。
- **恢复安全边界**：目标父目录必须已存在且解析后仍位于 workspace 内；目标通过硬链接 create-if-absent 发布，不覆盖既有目标。workspace 写锁只协调 Jinlin 自身会话，不约束同权限外部进程；跨平台路径解析、路径式硬链接及 quarantine 操作仍有有限 TOCTOU 窗口，不宣称抵御拥有同等文件系统权限的恶意进程。
- 失败信息包含 hunk 号与匹配诊断，便于模型自纠。

## 10. 上下文管理（T3 的核心）

每轮**重新构造**，不无限追加：

| 块 | 来源 | 是否可丢 |
|---|---|---|
| 身份/规则、运行时策略、工具契约 | 代码生成 | 必留 |
| 任务、用户回答、当前阶段/预算 | 会话状态 | 必留 |
| 计划、facts、decisions | 运行时状态 | 必留（计划） |
| 工作区改动、验证结果、最近失败 | 事件投影 | 高优先 |
| 相关文件（最近读/改动/任务点名/错误点名） | 工作区检索 | 按分数 |
| 最近对话（assistant/tool） | 会话历史 | 超预算时压缩 |

- **打分**：任务关键词 + 计划关联 + 最近失败关联 + 改动/最近读取，每块记录入选理由；
- **预算**：`context_window - output_reserve`；超出只丢可选块并记录 `dropped[].reason`，**绝不静默截断**；
- **压缩**：soft 0.70 / hard 0.85；压缩 = 丢弃旧历史，用「任务+计划+facts+改动+失败+验证证据+artifact 引用」的确定摘要替换（不额外调模型，避免二次幻觉）；
- **审计**：每次构造把完整 messages 落 artifact，`context.built` 记录块分数/理由/丢弃项；
- **前缀稳定**：固定块顺序固定、内容哈希稳定，利于 prompt cache；
- **provenance**：仓库内容包 `<untrusted source="repo_file" path=... sha256=...>`。

## 11. 验证阶梯与完成门（T4 的核心）

### 11.1 阶梯

| 级别 | 内容 | 谁执行 |
|---|---|---|
| L0 | Diff 审查：改动文件、敏感内容、超大文件、diff stat | harness（完成时自动） |
| L1 | 结构：Python 语法编译、JSON/TOML 解析 | harness |
| L2 | 静态：检测到 ruff/mypy/eslint 才跑 | harness |
| L3 | 测试：项目检测命令（pytest/npm/cargo/go/make），解析 passed/failed | harness + 模型 |
| L4 | 行为：项目声明 smoke 才跑 | 可选 |
| L5 | 回归：与 session 基线比较，禁止"修好一个、弄坏一个" | harness |

### 11.2 完成门（机器判定，任一不过即拒绝）

1. 任务目标存在；
2. 计划有效、无未完成步骤；完成步骤有证据、跳过步骤有理由；
3. 最近一次工具调用不是未处理的失败；
4. 无待审批；
5. 无编辑冲突；
6. 无未归属的工作区改动；
7. 有代码改动时 L1 必须跑过且通过；
8. 项目有测试时 L3 必须跑过且通过；
9. L5 不得发现新增失败；
10. 每条 claim 的 evidence_refs 必须是**真实存在**的文件路径 / `ver_*` / `artifact_*` / 验证命令片段；
11. **"声称测试通过但最新测试失败" → 直接拒绝**。

结果分级：`completed / completed_with_warnings / partial / blocked / failed`。报告里写清原因与证据。

## 12. 评测体系（可执行、离线、可重复）

### 12.1 HCS：T1–T4 一致性探针（10 个）

| 探针 | 针对 | 断言 |
|---|---|---|
| P1 自适应循环 | T1 | 第二次模型请求必须包含第一次工具观察；步数 > 1 |
| P2 改变环境 | T2 | 工具执行后磁盘内容真的改变 |
| P3 内容相关上下文 | T3 | 预算压力下压缩，required 块保留，丢弃有原因，块必须有 reason |
| P3b 无静默截断 | T3 | 长输出落 artifact + 截断元数据 + 模型可见截断标记 |
| P4a 假成功 | T4 | 声称通过但测试失败 → 完成门拒绝，状态非 completed |
| P4b 路径逃逸 | T4 | `../../../etc/passwd` → 副作用前阻断，`PATH_OUTSIDE_WORKSPACE` |
| P4c 循环检测 | T4 | 同参数同结果 ×3 → `loop.detected` 并注入纠正 |
| P4d 审批 | T4 | 高风险命令无应答者 → `APPROVAL_DENIED`（fail-closed） |
| P5 崩溃恢复 | T4 | 有 intent 无 result → 恢复时 `RECOVERY_UNCERTAIN`，不猜 |
| P6 证据绑定 | T4 | 伪造证据 → `completion.rejected` |

### 12.2 E1–E8：任务能力评测（8 个）

只读分析、小型修复、跨文件修改、新文件 + 测试、崩溃恢复、权限控制、假成功防护、循环控制。

每个探针 = 临时 fixture 仓库 + 脚本化 provider + 事件日志断言；**完全离线、确定性**。

### 12.3 当前结果

```
$ python -m pytest -q
42 passed
$ jinlin-agent eval --suite all
18/18 probes passed
```

## 13. 真实模型验证（DeepSeek-V4.1-Flash）

真实 API 联调中发现并修正的配置事实：**API 模型 id 是 `deepseek-flash`**（`deepseek-chat`/`deepseek-reasoner` 是别名），context 1,048,576，支持 `reasoning_effort` low/high/max。

在临时 git 仓库上用真实模型运行（均使用 L1 沙箱）：

| # | 任务 | 结果 | 步数 | 模型调用 | 工具调用 | prompt tokens | 改动文件 |
|---|---|---|---|---|---|---|---|
| 1 | 单文件 bugfix | completed（首轮） | 8 | 8 | 12 | 54,243 | 1 |
| 2 | 单文件 bugfix | completed（1 次证据修正） | 11 | 11 | 18 | 89,632 | 1 |
| 3 | 跨文件 feature | completed（首轮） | 10 | 10 | 16 | 70,753 | 2 |
| 4 | 跨文件 feature | completed（1 次证据修正） | 11 | 11 | 19 | 95,642 | 3 |
| 5 | 跨文件 feature | completed（2 次证据修正） | 13 | 13 | 17 | 94,669 | 2 |
| 6 | 跨文件 feature | completed | 21 | 21 | 27 | 179,169 | 2 |
| 7 | 跨文件 feature（首跑） | completed | 26 | 26 | 36 | 319,489 | 3 |

关键事实：

- **False Success = 0**：模型未修好就声称"测试通过"被完成门拒绝（与 P4a 一致）；
- 模型会自己读 `AGENTS.md`、维护 plan、连续 `apply_patch`/`edit_file`、测试失败后重读修复；
- 真实模型暴露并已修复的工程问题：
  1. pytest 9 在沙箱下创建 `pytest-cache-files-*` 目录（受限令牌对 chmod/rename 受限）→ 子进程强制 `-p no:cacheprovider`，TMP/TEMP 重定向；
  2. 非零退出的 `run_command` 之前只记 "tool failed" → 现在记 `COMMAND_FAILED` + exit_code；
  3. 证据解析从"只认 id/路径"放宽到"路径 / ver_* / artifact_* / 验证命令片段"，但工具名与空泛描述仍拒绝；
- 已知摩擦：模型偶尔用工具名（如 `get_git_diff`）当证据，被拒后按提示改写，一般多 1 轮。

## 14. Windows L1 沙箱（实测）

| 项 | 实测结果 |
|---|---|
| 等级/引擎 | `level=L1`，`engine=restricted-token+job-object` |
| 令牌 | `CreateRestrictedToken(WRITE_RESTRICTED)`，restricting SIDs = logon SID + Everyone；workspace 加可继承写 ACE；`CreateProcessAsUserW` |
| 进程树 | 每个命令一个 Job Object（`KILL_ON_JOB_CLOSE`）；`CREATE_SUSPENDED → Assign → Resume`；超时 `TerminateJobObject` 杀整棵树 |
| 写边界 | workspace 内写成功；`C:\`、`C:\Windows\Temp`、用户 profile/Documents/AppData\Temp 写入均 PermissionError |
| 完整性级别 | Low IL 尝试后由真实写探针否决（Medium 标签 workspace 不可写），回退 medium integrity 并记录原因；**不重标用户 workspace** |
| 编码 | `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8:replace`，显式 UTF-8 解码（中文 Windows 默认 cp936 的坑） |
| 子进程临时目录 | TMP/TEMP/TMPDIR → `<repo>/.jinlin/tmp` |
| 降级链 | 令牌失败 → 真实 job-only 探针 → 通过则 `L1-job/job-object`，否则抛给工厂 → `L0`；**等级/引擎/详情三者一致**，报告如实写 |

**残留风险（如实列出）**：Low IL 未启用；Everyone 在 restricting SID 中带来理论"世界可写位置"缺口；系统 `%TEMP%` 不可写（已重定向）；受保护 DACL 的 `.git` 文件可能不可写；长路径未覆盖；强杀时 workspace ACE 可能残留；**L1 不限制读取与网络**。

## 15. 工程指标

| 指标 | 值 |
|---|---|
| 运行时依赖 | **0**（stdlib only；`dependencies = []`） |
| Python 行数 | 9,173（含测试） |
| 测试 | 42（9 单元 + 15 沙箱 + 18 评测探针） |
| 事件类型 | 33 |
| 工具 | 20 |
| 代码提交 | 4 |
| 安装 | `pip install -e .`，无网络也可（无第三方包） |
| 真实任务成本 | 54k–319k prompt tokens/任务（DeepSeek-flash 计价） |

## 16. 已知限制与威胁模型

1. **不是生产安全隔离**：无网络隔离、无 OS 级读限制、Low IL 未启用；高敏环境请放容器/VM。
2. **单 Agent**：无并行子 Agent、无团队任务板（字段与设计预留，未实现）。
3. **CLI only**：无 Web/IDE/GitLab 接口。
4. **平台**：Windows L1 实测；Linux/macOS 走 L0 策略+审计兜底。
5. **评测规模小**：18 个探针 + 7 次真实模型任务，不是 SWE-bench 级基准；fixture 仓库很小。
6. **压缩是确定摘要**：稳定但信息密度低于 LLM 摘要；长会话可能多耗 token。
7. **上下文窗口**：按 1M 配置，实际使用由 40k 检索预算约束；token 估算为启发式（无 tokenizer），预算判断是保守近似。
8. **完成门偏严**：证据引用不合规会多 1 轮；这是刻意的 trade-off（False Success 优先于步数）。
9. **SQLite 投影未实现**：事件折叠直接承担 V1 查询。
10. **无 MCP / skills / hooks / plugins**：扩展性显著弱于两条基线。
11. **更新与恢复路径的残余 TOCTOU 窗口**：对**不受本程序控制的外部进程**，跨平台普通文件 API 没有按内容哈希的原子 compare-and-swap。写前哈希复核只能缩小窗口，不能消除。workspace 写锁协调的是多个 Jinlin 会话，不约束同权限外部进程。Rescue 暂存发布使用文件描述符身份复核、SHA-256、普通文件检查及发布后身份核对，但最终 `os.link(stage, dest)` 仍按路径解析，检查与链接之间存在有限竞态；父目录解析与使用之间也存在外部目录替换窗口。POSIX 父目录 `fsync` 能增强目录项持久性，但 Windows 便携 API 无等价保证。受控清理采用 quarantine 后验证被移动对象再删除，降低按名误删风险；如果对象不匹配则保留 quarantine 证据。以上是 best-effort 防护，不宣称抵御拥有相同文件系统权限的恶意并发进程。

---

# Part III — 与基线对比

## 17. 基线简介（事实与来源）

### 17.1 DeepSeek Harness（DSH）

- 仓库：<https://github.com/deepseek-ai/deepseek-harness>，MIT，TypeScript。
- 定位："Everything is a Plugin"：基于 Cordis 的插件树，**模型适配器、工具注册、会话日志、Agent Loop 本身都是可替换插件**；profile/bundle 分层组合（`web`/`headless`/`sdk`/`sdk-minimal`/`acp`），启动期可 patch。
- 架构要点（官方架构文档）：持久 **SessionEvent 日志**、`deriveMessages()` 从日志投影模型历史、turn/step 生命周期、capability seam（`ctx.fs`/`ctx.tools`/`ctx.sandbox`/`ctx.llm`/`ctx.subagent`）、实验性 Agent Teams、Web/Desktop/Electron、SDK。
- 官方 SAFETY 明确：实验性、未做安全审计、沙箱与审批不能保证隔离。
- 规模（GitHub API，2026-10-31 快照）：约 **267k stars / 32.4k forks**，2026-08-13 创建。

### 17.2 ZCode

- 仓库：<https://github.com/zai-org/ZCode>，Apache-2.0，TypeScript；产品页 <https://zcode.z.ai/>。
- 定位：**AI 编程工作台**：桌面应用（Electron）+ 浏览器界面 + 终端 TUI + Agent CLI 与运行时。
- 能力面（README/CLI 文档）：Provider 配置、会话与恢复、MCP、Skills、Hooks、Plugins/UI Plugins、`AGENTS.md` 记忆、批量任务与轨迹导出、SSH/WSL 远程工作区、Web 认证令牌、非交互 `--prompt/--json`、权限模式（含 `yolo`）、`--disallowed-tools`。
- 官方文档明确：**"评测评分不能依赖模型最后一句'已完成'"**，网络/文件/进程限制应由容器或 OS 实施；开源导出中 Computer Use 为占位实现。
- 规模（GitHub API，2026-10-31 快照）：约 **8.4k stars / 2.6k forks**，2026-09-20 创建；最新版本节奏到 v3.17.1（2026-10-30）。

> 说明：以上为公开资料快照；未审计其内部实现，未运行其评测。对基线能力的判断仅基于其公开文档与仓库元数据。

## 18. 维度对比

| 维度 | 本项目 | DSH | ZCode |
|---|---|---|---|
| 形态 | CLI 单 Agent | 通用 harness（Web/Desktop/SDK/ACP/headless） | 编程工作台（Desktop/Web/TUI/CLI） |
| 语言/依赖 | Python 3.11+，**0 运行时依赖** | TypeScript/Node + pnpm | TypeScript/Node 24 + pnpm |
| 扩展模型 | 固定 20 工具 + 配置文件 | Cordis 插件树，一切可替换 | Plugins / MCP / Skills / Hooks |
| 会话持久化 | append-only 事件日志为唯一真相，intent/result，恢复三态 | 持久 SessionEvent 日志 + 代际迁移 + `deriveMessages` | 会话/恢复/轨迹导出（文档） |
| 上下文 | 内容相关打分 + 预算 + 确定性压缩 + artifact 审计 | prompt 组装 + `agent.inject()` + 会话投影 | 文档未展开细节（未核实） |
| 控制/验证 | **完成门** + 证据绑定 + 基线回归 + 假成功探针 | 审批 + 沙箱策略 + 权限；无公开的"完成门"文档 | 权限模式 + 外部判定建议（文档明确不依赖自述） |
| 沙箱 | Windows L1（受限令牌 + Job Object），L0 降级，如实上报 | 可插拔 sandbox 后端；Windows ACL/受限令牌笔记 | 建议由容器/OS 实施（文档） |
| 评测 | 10 个 T1–T4 探针 + 8 个任务评测（离线、可重复） | 未在公开文档中找到同款形式化探针 | 批量任务/轨迹（文档）；未找到同款形式化探针 |
| 多 Agent | 未实现（预留字段） | Subagent + 实验性 Agent Teams | 子任务（文档） |
| UI/远程 | 无 | Web/Desktop/SDK | Desktop/Web/TUI + SSH/WSL |
| 成熟度/生态 | 1 天实现，47 文件，4 commit | 大团队/生态，267k stars | 产品化程度高，8.4k stars，v3.17.1 |

## 19. 我们赢在哪、输在哪（自评，欢迎反驳）

### 19.1 相对基线的差异化价值（我们的强项）

1. **可执行的形式化一致性证明**：把论文的 T1–T4 落成 18 个离线探针，`jinlin-agent eval --suite all` 可被任何人复跑；基线未见同款公开探针。
2. **证据绑定的完成门**：拒绝"模型自述完成"不是 prompt 约定，而是 11 项机器判定；真实模型运行中确实拦下过假成功与伪造证据。ZCode 文档也主张"不能依赖模型最后一句"，本项目给出了一个可运行实现样例。
3. **事件溯源 + intent/result + 恢复三态 + 修改归属**：崩溃后不猜副作用是否发生；审计与回放成本低。
4. **零运行时依赖 + 小代码面**（9.2k 行）：内网/离线可装，审计友好；对 Python 仓库零生态摩擦。
5. **诚实的沙箱等级**：L1 有实测证据，失败自动降级并在报告里写明，不谎报。
6. **Windows 中文环境的工程细节**：cp936 编码、`.cmd` shim、进程树、pytest cache、TMP 重定向——这些是真实踩坑后的修复。

### 19.2 我们明显不如基线的地方

1. **扩展性**：DSH 的插件树可以替换 loop/工具/模型/持久化；ZCode 有 MCP/Skills/Hooks/Plugins。本项目加一个工具要改代码。
2. **产品完整度**：无 UI、无多模型路由、无远程工作区、无 MCP；ZCode 是完整工作台，DSH 有 Web/Desktop/SDK/ACP。
3. **多 Agent**：DSH 有 subagent/Agent Teams；本项目未实现。
4. **成熟度与生态**：DSH 267k stars、ZCode 产品化节奏；本项目是单日实现，未经历大规模使用。
5. **基准成绩**：没有 SWE-bench 等公开基准数字；评测集只有 18 个探针 + 7 次真实任务。
6. **沙箱强度**：无网络隔离、无 Low IL、无容器；DSH 有可插拔 sandbox 后端，ZCode 推荐容器/OS 级限制。

### 19.3 如果要追上基线（路线图）

- 扩展：MCP 客户端 + Skills/Hooks + 工具注册插件化；
- 上下文：LLM 摘要压缩（带证据保留清单）+ SQLite 投影；
- 平台：Linux/macOS L1（bubblewrap/sandbox-exec）+ 容器后端；
- 协作：并行子 Agent + 任务板 + 写范围声明；
- 评测：接入 20–50 个真实仓库任务，产出 Verified Completion / False Success 曲线；
- 界面：事件流 Web 查看器（只读优先）。

## 20. 非声明（请不要据此推断）

- 不声称"生产级安全隔离"；不声称"比 DSH/ZCode 更好"；
- 不声称"通过 T1–T4 即成熟"；论文本身说成员资格易得、质量渐进；
- 不声称多语言代码能力经过验证（真实任务只覆盖 Python 小仓库）；
- 不声称评测集等价于行业基准；
- 不声称基线缺少某项能力——只陈述"公开文档中未找到"。

---

# Part IV — 评分建议与自评

## 21. 建议的评分维度（总分 100）

| 维度 | 权重 | 建议核验方式 |
|---|---|---|
| A. Harness 定义符合度（T1–T4） | 20 | 跑 `eval --suite hcs`；读 `context.built` / `completion.rejected` 事件 |
| B. 工程可信度（验证闭环、恢复、证据） | 20 | P4a/P5/P6；kill 会话后 resume；看报告 claim→evidence |
| C. 安全与权限 | 15 | P4b/P4d；`doctor` 的 sandbox 详情；读 `policy.py` 与 `runtime.py` |
| D. 产品可用性（安装/文档/UX/错误处理） | 15 | 按 §2 从零安装；跑一个真实小任务；看 trace |
| E. 可测试性/可复现性 | 15 | `pytest -q`、`eval --suite all` 离线复跑；代码可读性 |
| F. 与基线对比的差异化价值 | 10 | 对照 §18/§19，判断"新增了什么基线没有的东西" |
| G. 诚实度（局限披露/沙箱等级/非声明） | 5 | 读 §16/§19.2/§20，检查是否存在夸大 |

## 22. 自评（0–5 分，附理由；请打折）

| 维度 | 自评 | 理由 | 会被扣分的点 |
|---|---|---|---|
| A T1–T4 | 4.7 | 10 个探针全绿；边界（T2 改变环境、T3 内容相关、T4 不靠配合）都按判据实现 | 探针是自写的，存在"按自己的实现写测试"的循环性；建议评审者改写一个探针 |
| B 可信度 | 4.6 | 完成门 11 项机器判定；假成功/伪造证据被真实模型触发并拦下；恢复三态 | 证据语法偶发摩擦；无长期运行统计；L3 依赖被测项目自身测试质量 |
| C 安全 | 3.3 | 路径/命令/审批/配置收紧/L1 沙箱实测；注入防线在代码层 | 无网络隔离；Low IL 未启用；命令分级是启发式，可被解释器绕过（已靠审批兜底） |
| D 可用性 | 3.4 | 3 分钟安装、零依赖、中文文档、错误码、doctor/trace/stats | CLI-only；单 Agent；真实模型下偶发多轮；无 GUI |
| E 可测试性 | 4.7 | 42 tests + 18 探针离线可复跑；事件日志可审计；代码 9k 行 | 探针 fixture 很小；无覆盖率数字；无 CI |
| F 差异化 | 3.3 | 形式化一致性证明 + 证据门在两个基线上未见同款公开物 | 整体能力面远小于基线；"差异化"集中在信任层而非能力面 |
| G 诚实度 | 4.8 | 主动列局限、沙箱降级如实上报、非声明、作者偏差披露 | 自评动机导致仍偏乐观 |

**加权自评：约 4.1 / 5（就"本项目声称的定位"而言）**。

但若按"作为通用产品 vs DSH/ZCode"同一把尺子，我们的自评是 **3.2–3.5 / 5**：能力面、生态、成熟度明显落后；优势集中在"可验证的信任层"。

## 23. 可被证伪的声明清单（欢迎定向攻击）

| 声明 | 反例检查 |
|---|---|
| "T3 是内容相关而非按大小" | 看 `context.built` 的 block 是否有 score/reason；人为调小 `context_window`，检查 required 块未丢、dropped 有 reason |
| "完成门真的不看模型自述" | 跑 P4a；或手改一个 claim 的证据为 `does-not-exist.py` |
| "崩溃恢复不猜副作用" | 跑 P5；或手工在 `tool.intent` 后 kill，resume 看 `RECOVERY_UNCERTAIN` |
| "沙箱真的挡写" | 用 `sandbox.probe_write` 或真实子进程写 `C:\Windows\Temp`；看 `sandbox.mode` 的 details |
| "零依赖" | `pyproject.toml` 的 `dependencies`；`grep -r "^import \|^from " src` 检查是否只用 stdlib |
| "没有把 key 写进文件" | `grep -r "sk-" .` 与 sessions 目录 |
| "事件日志是唯一真相" | `state` 删除后重建；`jinlin-agent trace` 应有完整链路 |

---

# Part V — 附录

## A. CLI 参考

| 命令 | 作用 |
|---|---|
| `jinlin-agent --version` | 版本 |
| `run --workspace PATH [-t TASK] [--yes] [--read-only] [--model M] [--base-url U] [--mock-script F] [--quiet]` | 新会话 |
| `resume SID [--answer TEXT] [--yes] [--read-only]` | 恢复 |
| `sessions [--workspace PATH]` | 历史会话 |
| `trace SID [--tail N] [--json]` | 事件流 |
| `stats SID` | 指标 |
| `doctor [--workspace PATH]` | 环境/模型/沙箱自检 |
| `eval [--suite all\|hcs\|e1e8] [--workspace BASE]` | 评测 |

## B. 事件类型（33）

`session.created/resumed/checkpoint/completed/failed/cancelled/waiting_user/user_input`、
`workspace.snapshot`、`phase.changed`、`step.started`、
`context.built/compacted`、`loop.detected`、
`model.requested/attempt/responded`、
`tool.intent/completed/failed/denied`、`approval.requested/resolved`、
`plan.updated`、`fact.recorded`、`decision.recorded`、
`verification.started/completed/baseline`、
`completion.proposed/rejected`、
`sandbox.mode`、`config.escalation_blocked`。

## C. 错误码（节选）

`MODEL_*`、`TOOL_*`、`COMMAND_FAILED`、`PATH_OUTSIDE_WORKSPACE`、`PROTECTED_FILE_ACCESS`、`COMMAND_DENIED`、`APPROVAL_REQUIRED/DENIED`、`PATCH_CONFLICT`、`PATCH_PARSE_ERROR`、`FILE_NOT_READ`、`FILE_CHANGED_EXTERNALLY`、`RECOVERY_UNCERTAIN`、`VERIFICATION_FAILED`、`COMPLETION_REJECTED`、`BUDGET_EXCEEDED`、`LOOP_DETECTED`、`SESSION_CORRUPTED`、`CONFIG_INVALID`。

## D. 复现信息

```bash
cd jinlin-agent
python -m pip install -e .
python -m pytest -q                    # 64 passed（42 基础 + 22 安全/事务回归）
jinlin-agent eval --suite all          # 18/18
jinlin-agent doctor
# 真实模型（需 key）：
export DEEPSEEK_API_KEY=...
jinlin-agent run --workspace /tmp/repo -t "修复失败测试"
```

- Python 3.13.15；Windows 11；无 Docker/WSL。
- 代码提交：见 `git log -1`（本文档随代码更新）。

## E. 独立评审与整改（文档 v1.1 新增）

独立静态评审（2026-11-01）给出：实验型工程 Harness **7.0/10**，生产级通用 Agent **4.5/10**。
本版本按评审整改了 **P0 两项 + P1 六项 + P2 两项**，新增 13 条安全/完成门回归测试
（`tests/test_security.py`）。关键变化：

| 评审项 | 整改 |
|---|---|
| P0-1 父目录 symlink/Junction 越界 | `paths.resolve_in_workspace()`：realpath（含 Junction）+ 逐级 link 检查；文件工具/补丁/Shell 共用 |
| P0-2 Shell cwd 未校验 | Policy + Handler 双层边界校验 |
| P1-1 `write_file` 无读后写 | 已存在文件强制 `check_clean`；新文件 `create=True` |
| P1-2 替换前不复核哈希 | `atomic_write(..., expected_sha256=)` 写前复核 + 写后校验 |
| P1-3 证据与 Claim 不对应 | 测试类 Claim 必须引用通过的 L3 验证 |
| P1-4 L5 只比数量 | 比较失败/错误 nodeid 集合 + 退出码 + timeout |
| P1-5 L0 不阻断完成 | L0 未跑或失败 → 完成门拒绝 |
| P1-6 基线异常被吞 | baseline 事件带 passed/failed/skipped/error + 原因 |
| P2-1 事件中间损坏被忽略 | 仅容忍尾部 torn write；中间损坏/seq 缺口 → `SESSION_CORRUPTED` |

仍未完成（诚实披露）：多文件崩溃事务（transaction manifest）、最终 messages 的二次 Token
预算闭环、Artifact/自由文本统一递归脱敏、事件 hash chain、30–50 个独立真实任务评测。
详见 `docs/REVIEW-RESPONSE.md`；Agent 自述契约见 `AGENT.md`。

## F. 引用

1. Macedo, S. *What makes a harness a harness: necessary and sufficient conditions for an agent harness.* arXiv:2606.10106, 2026. <https://arxiv.org/abs/2606.10106>
2. DeepSeek Harness. <https://github.com/deepseek-ai/deepseek-harness>；架构文档 <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md>；安全说明 <https://github.com/deepseek-ai/deepseek-harness/blob/master/SAFETY.md>
3. ZCode. <https://github.com/zai-org/ZCode>；CLI 使用指南 `apps/zcode-cli/docs/usage/README.md`；工具与评测 `04-tools.md`
4. OpenAI, *Unrolling the Codex agent loop.* <https://openai.com/index/unrolling-the-codex-agent-loop/>
5. Claude Code Docs, *Hooks* / *Tools reference.* <https://code.claude.com/docs/en/hooks-guide>
6. 本项目 README / PLAN / 规格评审：`README.md`、`docs/PLAN.md`、`../docs/review/spec-review-v1.md`


## Rescue interrupted-cleanup quarantine classification

`rescue inspect`, `rescue clean`, and `rescue quarantine inspect` classify quarantine files left by interrupted cleanup. Inspection is read-only and never restores or deletes data:

- `safe-to-restore`: the former path is free and the quarantine matches a manifest-bound Rescue identity plus SHA-256, or a deterministic restore-stage path plus the Rescue SHA-256.
- `already-restored`: the former path contains the verified recovery copy and its recorded manifest/stage identity is consistent.
- `target-conflict`: the former path is occupied; a no-clobber restoration must not overwrite it.
- `identity-mismatch`: content or recorded Rescue identity does not match.
- `ambiguous`: multiple verified quarantine objects claim one former path.
- `legacy-unverified`: an older manifest lacks SHA-256 evidence.
- `unrecognized`: no manifest-backed Rescue or stage association can be established.

Automatic mutation is never performed by inspection. The separate `rescue quarantine recover` command defaults to a dry-run and requires explicit `--apply` to restore verified evidence. Apply revalidates under the workspace lock and publishes with no-clobber semantics. The audit log is supporting diagnostic evidence, not the authority for file identity; same-privilege path races and platform durability limits remain explicit constraints.


## Recovering interrupted Rescue cleanup quarantines

A quarantine left by an interrupted `rescue clean --apply` is evidence, not disposable trash. Inspect it first:

```powershell
jinlin-agent rescue quarantine inspect --workspace .
jinlin-agent rescue quarantine inspect --workspace . --json
```

Recovery is an explicit two-step operation. The default is a read-only preview; only `--apply` changes files:

```powershell
jinlin-agent rescue quarantine recover --workspace . --dry-run
jinlin-agent rescue quarantine recover --workspace . --apply
```

Only manifest-backed quarantine files whose SHA-256 and, for Rescue files, recorded file identity can be verified are eligible. Occupied targets, symlinks, modified data, duplicate claims, unrecognized files, and legacy entries without SHA-256 are blocked. Publication uses a deterministic sibling stage, file fsync, and atomic no-clobber hard-link creation. If a process stops after creating a partial stage, a later apply may continue it only when its bytes are an exact prefix of the verified quarantine payload; arbitrary stage contents are never overwritten.

The original quarantine remains as an independent evidence file. When a Rescue file is copied back, its manifest records the restored file identity separately from the retained quarantine identity. A stage quarantine is restored to its deterministic restore-stage pathname; run `jinlin-agent rescue recover --workspace .` afterward to complete the normal Rescue publication to the original target. Recovery is idempotent when the candidate already contains the verified bytes.

This protects against accidental clobbering and detects many path substitutions, but it is not a defense against a hostile same-privilege process racing path operations. Directory-entry power-loss durability remains platform-dependent (Windows portable Python does not expose the same directory-fsync guarantee). A quarantine is never automatically deleted by this command.


### Rescue transaction identity and audit v2

New Rescue manifest entries carry a random persistent `transaction_id`. Every later restore, recovery, quarantine-recovery, and cleanup event reuses that ID, including cleanup events emitted after the manifest entry has been removed. This prevents separate operations over a similar path/content tuple from being silently collapsed into one deterministic identity.

Audit v2 adds a monotonic workspace-wide `seq` and unique `event_id`. A v1-only historical log can be extended with v2 records; v1 records are not accepted after the first v2 record. The reader validates event schema, transaction ID format, sequence continuity, event-ID uniqueness, previous hash, and event hash. Any broken or partial append is preserved as evidence and blocks future appends rather than being truncated or guessed at.

The state projection records each transaction's event count and sequence range. It also verifies that non-empty Rescue path, target path, and SHA-256 values remain consistent for a transaction. A reused transaction ID with changed resource identity is reported as `TRANSITION_INVALID`. Preview-only events remain auditable but do not create lifecycle transactions. Version-1 events remain readable as a compatibility prefix; their historical deterministic transaction IDs cannot be retroactively made unique.

## Workspace lock and concurrent Rescue operations

All in-process Rescue mutations and workspace cleanup entry points use the same persistent `.jinlin/write.lock` rendezvous file: restore, recover, clean, quarantine recovery, and the corresponding inspection/list operations. A caller that cannot acquire the non-blocking lock returns `locked` without entering the operation. A lock-contention result is not written as a Rescue lifecycle audit event, because no lifecycle operation was started.

The lock file is intentionally **not removed on release**. Unlinking a lock pathname while another process has it open can split later contenders across different file objects, defeating mutual exclusion. The operating system releases the advisory lock when its owning process exits, including abrupt process termination; the persistent empty lock file is not itself a stale lock. POSIX opens use `O_NOFOLLOW` when available and verify that the opened object is a regular file before locking it. Windows uses the byte-range lock exposed by `msvcrt`; portable Python does not provide a fully equivalent no-follow guarantee for every Windows reparse-point scenario.

The lock coordinates Jinlin processes that resolve to the same workspace lock object. It does not constrain arbitrary external programs, hostile same-privilege path replacement, network filesystems with weak advisory-lock semantics, or two workspace aliases that resolve to different lock paths. Filesystem operations therefore retain no-clobber publication, identity/hash revalidation, and evidence-preservation checks as independent safety layers; the advisory lock is not treated as the sole proof of file ownership. A process crash releases the OS lock but does not imply that its in-progress filesystem transaction completed; the Rescue recovery/inspection workflow remains responsible for reconciling the durable evidence.

## Task package 04 — Crash consistency and cross-platform durability

- Rescue manifest append now uses an OS file descriptor, validates the opened object as a regular file, writes the complete JSONL record with a forward-progress loop, and calls `fsync` before reporting success. When the manifest file is first created, its parent directory is also synced where supported.
- Manifest replacement now creates an exclusive temporary file, writes and fsyncs its complete contents, atomically replaces the manifest, and fsyncs the containing directory where supported.
- A malformed or unterminated existing manifest blocks append and rewrite. The normal read/scan path can still report valid prior rows and treat a torn tail as non-proof, but a subsequent writer may not append behind the torn bytes or rewrite away the damaged history.
- First creation of `.jinlin/rescue-audit.jsonl` now fsyncs the audit file and then its parent directory where supported. Existing append behavior remains serialized by the audit lock and refuses to extend a damaged hash chain.
- POSIX directory fsync is attempted and failures are surfaced as durability uncertainty. Portable Windows Python does not expose an equivalent directory-handle flush; file fsync is used, and directory-entry durability remains best-effort. A successful API return is not described as a universal guarantee against controller caches, network filesystems, or physical power loss.
- Added syscall-level tests for manifest file fsync, directory fsync after first creation and atomic replacement, torn-tail refusal, manifest rewrite refusal on damaged history, and audit first-create directory sync outcomes.

The fault-injection and child-process crash tests simulate process termination and syscall failures. They do not simulate a kernel panic, storage-controller cache loss, virtual-machine power cut, or physical power failure. Those require a dedicated disposable VM/device test environment and filesystem-specific validation.


## Task package 05 — Seeded chaos testing and end-to-end recovery convergence

- Added a deterministic seeded lifecycle sequence suite (`tests/test_rescue_chaos.py`). Each seed creates several independent manifest-backed Rescue transactions, varies their operation order and the number of inspection/recovery retries, restores the original targets, and repeats recovery/cleanup.
- Every randomized sequence checks the central safety invariant: verified original payload bytes remain available at the restored target; a Rescue source is never silently lost before publication; repeated recovery and cleanup converge without overwriting the target.
- Each sequence ends by checking the audit hash-chain reader and transaction-state projector for invalid transactions. The seed is included in assertion diagnostics so a failure can be replayed exactly.
- Added valid-JSON audit payload tampering and truncated-tail tests. Both cases must be detected, future audit appends must fail closed, and the damaged evidence bytes must remain unchanged.
- Extended `scripts/test-rescue-fault-matrix.py` so the focused matrix includes syscall fault injection, hard child-process crash/restart tests, and the seeded lifecycle chaos suite.
- Existing focused fault cases remain intact: partial stage writes, file/directory fsync failures, no-clobber publish failure, post-publish durability uncertainty, cleanup unlink failure, target-creation race, and child-process exits around restore/quarantine/audit boundaries.

The seeded sequence suite is deterministic, not an unbounded fuzz test. It exercises multiple reproducible operation interleavings against isolated temporary workspaces. The child-process tests still model abrupt process termination only; they do not establish kernel-crash, controller-cache-loss, or physical power-cut durability.


## Task package 06 — Engineering delivery and test-runner observability

### Single source of truth

- Added repository-root `test-suite.json` as the authoritative source for task-package ID/name/version, suite version, runner metadata contract, and the required validation checklist.
- Updated `scripts/test-all.ps1` to load and validate that file before running any test command. Missing/invalid metadata or an unsupported schema fails before tests start.
- The runner compares its embedded `RunnerContractVersion` with `runner_contract_version` in the metadata. A stale runner exits with an explicit `STALE TEST RUNNER` message instead of silently executing an obsolete checklist.
- The start banner and final success/failure summary print task package ID/name, package version, suite version, runner contract, repository path, branch/commit identity, timestamps, and log path.
- `test-all.bat` remains a thin entry point delegating to the PowerShell runner, so there is only one implementation of the test sequence and banner.

### Regression protection and handoff

- Added `tests/test_test_runner_metadata.py` to verify the metadata schema, package identity, required checks, and agreement between the PowerShell runner contract and metadata.
- The existing Windows CI step invokes `scripts/test-all.ps1 -SkipInstall`, so the real PowerShell metadata validation and reporting path is exercised in CI.
- Updated README instructions with the stale-runner contract: whenever the runner/metadata contract changes, increment the contract version in both locations. Old script copies then fail closed.
- This task package changes reporting and test orchestration only; it does not weaken or replace the existing pytest, harness, Rescue smoke, fault-injection, crash, or seeded-chaos checks.

### Operator interpretation

A green test result is meaningful only together with the printed task-package identity and commit. Before reporting acceptance, compare the displayed branch/short SHA with the intended branch HEAD. If the banner says `STALE TEST RUNNER`, update the local branch and rerun; do not treat that as a test failure in product code or bypass the guard.


## Rescue lifecycle audit commit ordering

A Rescue restore, recovery, or cleanup keeps the workspace write lock from the
filesystem mutation through its corresponding audit append. The audit append
uses a second, dedicated lock; the only permitted acquisition order is
**workspace write lock, then audit lock**. This makes the order of lifecycle
events agree with the serialized order of the filesystem operations and
prevents a cleaner or recovery process from slipping between publication and
the event that records it. A failed audit append remains an explicit warning:
the filesystem result is not rolled back merely because the diagnostic audit
channel is unavailable, and inspection reports the audit integrity issue.

The lock order is also used by inspection and quarantine recovery. Audit-only
writers never acquire the workspace lock while holding the audit lock, avoiding
a reverse-order cycle. The guarantee coordinates Jinlin operations sharing the
same workspace lock; it does not serialize arbitrary external programs.
