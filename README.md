# Jinlin Code Engineer Agent

[![ci](https://github.com/hejinlin0824/jinlin-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/hejinlin0824/jinlin-agent/actions/workflows/ci.yml)

> 一个**事件溯源、可验证、可恢复、带沙箱**的代码工程 Agent Harness。
> CLI 直接可用，不是演示脚本：每条"完成"都有可复查的证据。

📄 完整技术报告 + 产品手册（含评分建议与 DSH/ZCode 基线对比）：[`docs/REPORT-AND-HANDBOOK.md`](docs/REPORT-AND-HANDBOOK.md)
🤖 **项目交接手册（接手 AI 必读）**：[`AGENT.md`](AGENT.md) — 背景、当前状态、三轮评审、架构细节、操作手册、待修 P1、踩坑清单
🛠 独立评审响应与整改记录：[`docs/REVIEW-RESPONSE.md`](docs/REVIEW-RESPONSE.md)
📋 整改提交报告（供审查）：[`docs/REVIEW-SUBMISSION.md`](docs/REVIEW-SUBMISSION.md)

```bash
python -m jinlin_agent doctor                      # 环境/模型/沙箱自检
export DEEPSEEK_API_KEY=sk-...
python -m jinlin_agent run --workspace ./my-repo -t "给查询接口加分页，不改数据库结构"
```

---

## 1. 它是什么

一个运行在本地代码仓库里的软件工程智能体。它能：

- 调查仓库（项目类型、指令文件、相关文件与符号）；
- 建立并维护**运行时计划**（不是展示文本）；
- 用补丁协议修改代码（`apply_patch` V4A / `edit_file`，读后写 + 哈希校验）；
- 执行测试、构建、Lint，并根据真实失败继续修复；
- 在**完成门**通过后才宣布完成（假成功会被拒绝）；
- 全程写入 append-only 事件日志，程序中断后可恢复；
- 输出带证据链的中文报告。

它不是什么：不是多 Agent 公司、不是 IDE、不是"支持 100 个工具"的工具集市、不自动 commit/push。

---

## 2. 为什么它是 Harness（arXiv:2606.10106 的 T1–T4）

| 条件 | 本项目如何满足 | 可执行证据 |
|---|---|---|
| **T1 自适应 Agent Loop** | `loop.py`：构造上下文 → 模型 → 解释 → 派发 → 观察回灌 → 下一步；观察真的改变下一步 | HCS `P1`：第二次请求必须包含第一次观察 |
| **T2 改变环境的工具接口** | 文件读写、补丁、Shell（argv 数组）、Git、后台作业，副作用全部可观察 | HCS `P2`：磁盘文件内容真的变了 |
| **T3 主动上下文管理** | 每轮重建上下文；进入/退出由任务相关性打分；超预算只丢可选块并记录理由；压缩用状态摘要替换历史，**绝不静默截断** | HCS `P3`/`P3b`：压缩后关键块仍在、丢弃有原因、长输出落 artifact |
| **T4 不依赖模型配合的控制** | 路径策略、命令风险分级、fail-closed 审批、步数/失败/循环上限、验证阶梯、完成门、证据绑定 | HCS `P4a/P4b/P4c/P4d/P5/P6` + E1–E8 |

> 论文明确"四条件是成员资格测试，不是质量度量"。所以本仓库用 **18 个可执行探针**同时证明"是 harness"和"不是玩具"：`jinlin-agent eval --suite all`。

当前状态：**18/18 通过**（见 §9）。

---

## 3. 快速开始

要求：Python 3.11+（开发用 3.13）；**零第三方运行时依赖**。

```bash
git clone https://github.com/hejinlin0824/jinlin-agent.git && cd jinlin-agent
python -m jinlin_agent doctor

# 模型：DeepSeek OpenAI-compatible（默认 deepseek-flash）
export DEEPSEEK_API_KEY=sk-...
# 或写入 ~/.jinlin-agent/config.toml 的 [model]（key 只从环境变量/凭据读取）

python -m jinlin_agent run --workspace /path/to/repo -t "修复失败测试"
```

交互式审批：中/高风险操作会询问 `[y]/[a]/[N]`。非交互环境默认 **fail-closed**（拒绝），`--yes` 自动批准除 destructive 之外的操作，`--read-only` 只读。

常用命令：

```bash
jinlin-agent sessions                       # 历史会话
jinlin-agent resume <session_id> [--answer "..."]   # 恢复（崩溃/等待用户后）
jinlin-agent trace <session_id> --tail 60   # 事件流审计
jinlin-agent stats <session_id>             # 指标（步数/token/验证/压缩）
jinlin-agent eval --suite all               # HCS + E1-E8 评测
jinlin-agent clean --workspace .              # 只预览可证明归属的普通残留文件
jinlin-agent rescue list --workspace .        # 列出通过 manifest + SHA-256 校验的救援副本
jinlin-agent rescue restore <rescue-path> --workspace .  # 暂存后原子无覆盖发布
jinlin-agent rescue recover --workspace .                # 检查并恢复中断的暂存事务
jinlin-agent rescue clean --workspace .                  # 预览可安全清理的冗余证据
jinlin-agent rescue clean --apply --workspace .          # 显式清理已验证的 stage / Rescue 副本
jinlin-agent rescue inspect --workspace .                # 只读检查事务状态、审计链和遗留证据
```

安装为命令行（可选）：

```bash
python -m pip install -e .
jinlin-agent --help
```

---

## 4. 编辑协议

编辑是 code harness 的生命线，本项目只允许两种通道，共用一个安全引擎：

1. **`apply_patch`（主协议，Codex V4A 信封）**——多文件、原子提交：

```
*** Begin Patch
*** Update File: src/app.py
@@ optional anchor
 context line (starts with one space)
-removed line
+added line
*** Add File: tests/test_new.py
+...
*** Delete File: old.py
*** End Patch
```

2. **`edit_file`（兼容 Anthropic Edit 风格）**——单文件精确替换，`old_string` 必须唯一。

共同规则（由 harness 强制执行，不靠提示词）：

- **精确匹配**，不做模糊匹配（静默改错比失败更糟）；
- **读后写**：会话内没读过、或读后文件被改过 → 拒绝，回传诊断与"重新读取"提示；
- **全有或全无**：多文件补丁先全部校验，再原子替换；失败自动回滚（先移动后校验，不覆盖并发内容）；
- **保留 BOM 与换行风格**（Windows CRLF 不会因为 `\n` 匹配而全量 miss）；
- 新建文件用 `O_CREAT|O_EXCL` 独占创建（文件系统级原子 no-clobber，绝不覆盖并发创建者）；
- 更新用临时文件 + `os.replace` + 写前复核哈希，**缩小**（不消除）TOCTOU 窗口；写阶段持 workspace 写锁串行化多个 Jinlin 会话；对外部进程的残余窗口见 `docs/REPORT-AND-HANDBOOK.md` §16。

---

## 5. 安全模型（诚实版）

| 层 | 内容 |
|---|---|
| 路径 | 工作区边界（含符号链接逃逸/大小写/编码）、受保护文件（`.env`/密钥/`.git`）拒绝 |
| 命令 | argv 数组、不做 `shell=True`、Windows `.cmd` shim 显式拒绝或解析为真实入口；风险分级（low/medium/high/destructive/network） |
| 审批 | 与具体参数绑定；`ask` 无应答者时**拒绝**（fail-closed）；session 级授权可控 |
| 配置 | 内置 < 用户级 < 项目级；**项目级只能收紧**（仓库放一行 `allow_delete=true` 不会生效，只会被记录） |
| 沙箱 | `L1`：Windows Job Object 杀进程树 + 受限令牌（写限制）；不可用时**降级 `L0` 并在报告里如实标注**，绝不假装 |
| 注入 | 仓库内容一律 `<untrusted>` 包装；权限由代码判定，提示词无法提权 |

本机实测（Windows 11，标准用户）：`level=L1`、`engine=restricted-token+job-object`；workspace 内写成功；`C:\`、`C:\Windows\Temp`、用户 profile/Documents/AppData\Temp 写入均 `PermissionError`；超时/关停用 Job Object 杀整棵树。子进程的 `TMP/TEMP/TMPDIR` 被重定向到 `<workspace>/.jinlin/tmp`（L1 下系统临时目录不可写，否则 pytest/tempfile 会无故失败）。详见 `src/jinlin_agent/sandbox/windows.py` 头部注释与 `tests/test_sandbox_win.py`。

> V1 的边界：网络访问没有防火墙级隔离（命令策略 + 审批控制），生产凭据不会被读取，但请把它当作"有守卫的本地工具"，而不是生产隔离环境。

---

## 6. 持久化、恢复与归属

- **唯一真相**：`sessions/<id>/events.jsonl`（append-only、`seq` 单调、每次 fsync、单写者文件锁）。SQLite 等在路线图上只是投影，不参与判定。
- **先 intent 后副作用**：工具调用先写 `tool.intent`（参数哈希 + 幂等键），再执行，再写结果。崩溃后恢复对"有 intent 无 result"的调用分三态：只读→安全忽略；写类→标记 `RECOVERY_UNCERTAIN` 并提示先核对工作区；不可判定→等待用户。
- **修改归属**：session 快照记录任务开始前已有的脏文件；完成门区分"用户既有修改 / Agent 修改 / 外部程序修改"，出现未归属改动会阻断完成。
- **Checkpoint**：每 N 步写状态哈希与序号；状态永远可由事件重放重建。

---

## 7. 上下文（T3 的实现细节）

每轮重新构造，而不是无限追加：

- **固定块**：身份/规则、运行时策略、工具契约、项目指令、任务、当前阶段与计划、工作区状态。
- **检索块**：最近读过的文件、本次改动、任务/计划/错误里点名的文件，按 `任务关键词 + 计划 + 最近失败 + 改动` 打分排序。
- **预算**：`context_window - output_reserve`；required 块永不丢；可选块按分数进入，被丢弃的块记录原因。
- **压缩**：超过 soft 阈值后用"任务 + 计划 + facts + 改动 + 失败 + 验证证据 + artifact 引用"的摘要替换旧历史；硬阈值下进一步收紧。
- **审计**：每次构造都把完整 messages 落 artifact，`context.built` 事件记录每个块的分数/理由/丢弃项。

---

## 8. 验证阶梯与完成门

模型只能说 `propose_completion`，**永远不能自己把状态设为完成**。

验证阶梯（完成时由 harness 自动执行，不依赖模型自觉）：

| 级别 | 内容 |
|---|---|
| L0 | Diff 审查：改动文件、敏感内容、超大文件、git diff stat |
| L1 | 结构：Python 语法编译、JSON/TOML 可解析（本次改动的文件） |
| L2 | 静态：项目检测到 ruff/mypy/eslint 等才运行 |
| L3 | 测试：项目测试命令（支持 `python -m pytest`、npm、cargo、go、make），解析通过/失败数 |
| L5 | 回归：与 session 开始时的基线比较，禁止"修好一个、弄坏一个" |

完成门（全部机器判定）：任务存在、计划无未完成步骤（完成需证据、跳过需理由）、无未处理工具错误、无待审批、无编辑冲突、无未归属改动、验证层级适当且通过、每条 claim 的证据真实存在、**"声称测试通过但最新测试失败"直接拒绝**。

结果不是二值：`completed / completed_with_warnings / partial / blocked / failed`，报告里写明原因。

---

## 9. 评测（当前全绿）

```bash
python -m jinlin_agent eval --suite all
```

- **HCS（T1–T4 一致性）**：P1 自适应循环、P2 环境改变、P3 内容相关上下文+压缩、P3b 无静默截断、P4a 假成功拒绝、P4b 路径逃逸、P4c 循环检测、P4d fail-closed 审批、P5 崩溃恢复、P6 证据绑定。
- **E1–E8（任务能力）**：只读分析、小型修复、跨文件修改、新文件+测试、崩溃恢复、权限控制、假成功防护、循环控制。

每个探针都是"临时 fixture 仓库 + 脚本化模型 + 事件日志断言"，可离线重复运行。

### 真实模型验证（DeepSeek-V4.1-Flash，2026-10-31）

- 真实模型 id 是 **`deepseek-flash`**（`deepseek-chat` / `deepseek-reasoner` 是同一模型的别名）；context 1,048,576，`reasoning_effort` 支持 low/high/max，本项目默认 `high`、`temperature=0`。
- 在临时 git 仓库上用真实 key 跑了多轮任务，全部到达 `completed`：
  - **单文件 bugfix**：8 步 / 12 次工具调用，一次通过（读测试 → 读实现 → apply_patch → pytest → 完成门）。
  - **跨文件 feature**（config.py + tests + 保留 load 接口）：10–21 步 / 16–27 次工具调用；模型会自己读 `AGENTS.md`、维护 plan、连续 `apply_patch`/`edit_file`，并在测试失败后自行修复。
  - **False Success = 0**：模型未修复就声称"测试通过"时被完成门拒绝（与 HCS P4a 一致）。
- 真实模型暴露并已修复的问题：
  - pytest 9 在沙箱下创建 `pytest-cache-files-*` 目录（受限令牌对 chmod/rename 受限）→ 子进程强制 `-p no:cacheprovider`，TMP/TEMP 重定向到 `<repo>/.jinlin/tmp`；
  - 非零退出的 `run_command` 之前只记 "tool failed" → 现在事件里记录 `COMMAND_FAILED` + exit_code；
  - 完成门证据解析从"只认 id/路径"放宽到"文件路径 / ver_* / artifact_* / 验证命令片段"，但工具名和空泛描述仍会被拒绝。
- 已知摩擦：模型偶尔把工具名（如 `get_git_diff`）当证据引用，会被门禁拒绝并提示改写；这是刻意的证据约束，代价是偶尔多 1 轮。
- API key 只从环境变量读取；已用 grep 验证事件日志、artifact、报告里都不出现明文 key。

---

## 10. 目录

```
src/jinlin_agent/
  cli.py         命令行（run/resume/trace/stats/doctor/eval/sessions）
  loop.py        Agent Loop + 完成流程
  context.py     内容相关上下文构建/压缩（T3）
  prompt.py      系统提示装配 + untrusted 包装
  model/         Provider 协议 + DeepSeek(OpenAI 兼容) + mock
  tools/         read/list/find/search/info、apply_patch/edit_file/write_file、
                 run_command/后台作业、git、plan/fact/decision/completion、项目摘要
  policy.py      路径/命令策略 + 审批（fail-closed）
  sandbox/       Windows L1（受限令牌+Low IL+Job Object）/ L0 降级
  verify.py      检测、基线、验证阶梯、完成门
  report.py      证据绑定报告
  runtime.py     唯一工具执行管线
  events.py      事件日志（唯一真相）
  state.py       事件折叠 → SessionState
  session.py     会话生命周期/锁/快照/恢复
docs/PLAN.md     实施计划与决策记录（D1–D12）
tests/           单元 + 18 个评测探针
```

## 11. 配置

`~/.jinlin-agent/config.toml`（用户级）与 `<repo>/.jinlin/config.toml`（项目级，只能收紧）：

```toml
[model]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"

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

## 12. 已知限制（V1）

- 真实模型验证已完成（DeepSeek-V4.1-Flash，7 次任务，False Success = 0），但样本量小、只覆盖小型 Python 仓库；详见 `docs/REPORT-AND-HANDBOOK.md` §13。
- 单 Agent；多 Agent 的字段与任务板只预留，不实现。
- 网络无 OS 级隔离；沙箱写限制依赖 Windows 受限令牌，构造失败会降级 L0（报告会写明）。
- 上下文压缩用确定性状态摘要，不做 LLM 摘要（更稳，但长会话信息密度略低）。
- 多文件补丁有回滚与写前哈希复核，但**跨文件崩溃原子性**未实现（无 transaction manifest）。
- 脱敏只覆盖事件参数与常见凭据形态；Artifact/报告尚未统一递归脱敏。
- SQLite 投影未实现（事件折叠足够 V1 规模）；无 Web/IDE/GitLab 接口。
- 以上限制与独立评审整改状态见 `docs/REVIEW-RESPONSE.md`。

## 13. 开发

```bash
python -m pytest -q            # 单元 + 18 评测探针
python -m jinlin_agent eval --suite all
```

约定：零第三方运行时依赖；所有副作用必须落事件；工具必须返回可序列化结果；先写测试再改行为。


### Rescue 事务审计与一键验收

每次 Rescue restore / recover / clean / quarantine-recover 都会尝试向 `.jinlin/rescue-audit.jsonl` 追加 SHA-256 hash-chain 事件。新事件采用 v2 格式，包含 manifest 中持久化的事务 ID、全局连续序号和唯一事件 ID；读取时兼容历史 v1 前缀，并验证序号、事件 ID、字段格式和 hash chain。事务状态投影还会检查状态迁移及同一事务的资源身份一致性。审计日志是追加式证据，不是恢复数据的权威来源；日志损坏时系统会报告完整性问题并拒绝继续追加，不会自动修复或覆盖旧日志。

`rescue inspect` 是只读检查命令，汇总已验证 Rescue、暂存文件、孤立暂存、清理中断 quarantine、事务状态和审计链完整性。事务状态由审计事件投影得到：`STAGED`、`PUBLISHED`、`CONFLICT`、`UNCERTAIN`、`ORPHANED`、`CLEANED` 等；它不替代文件内容与 manifest 的实际校验。

Windows 一键验收：

```powershell
.\test-all.bat
```

也可以在 PowerShell 中运行 `./scripts/test-all.ps1`。脚本会安装 editable dev 依赖、执行全量 pytest、Harness eval、Rescue 生命周期 smoke test 和故障注入矩阵。故障矩阵可单独运行：`python scripts/test-rescue-fault-matrix.py`。除 syscall 故障注入外，矩阵还启动独立 Python 子进程，在 stage 创建、部分写入、文件同步、目录同步、hardlink 发布和 cleanup quarantine 后调用 `os._exit()`，随后由父进程重启恢复并检查证据。它模拟进程突然终止，不等价于内核崩溃或断电测试。所有测试只在 pytest 临时目录创建隔离工作区，不会在当前仓库里创建或删除测试 Rescue 文件。日志写入系统临时目录。


### Rescue cleanup recovery diagnostics

`jinlin-agent rescue inspect` classifies interrupted cleanup quarantine evidence using manifest identity, SHA-256, deterministic restore-stage association, former-path occupancy, and duplicate claims. Possible classifications are `safe-to-restore`, `target-conflict`, `identity-mismatch`, `ambiguous`, `unrecognized`, and `legacy-unverified`. This is strictly read-only: it does not restore, overwrite, or delete quarantine evidence.


Machine-readable inspection is available with `jinlin-agent rescue inspect --workspace ./my-repo --json`; it emits the full report (including quarantine classifications and counts) and preserves the same exit-code semantics as text mode.


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
