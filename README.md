# Jinlin Code Engineer Agent

> 一个**事件溯源、可验证、可恢复、带沙箱**的代码工程 Agent Harness。
> CLI 直接可用，不是演示脚本：每条"完成"都有可复查的证据。

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
git clone <this-repo> && cd jinlin-code-agent
python -m jinlin_agent doctor

# 模型：DeepSeek OpenAI-compatible（默认 deepseek-v4.1-flash）
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
- **全有或全无**：多文件补丁先全部校验，再原子替换；失败自动回滚；
- **保留 BOM 与换行风格**（Windows CRLF 不会因为 `\n` 匹配而全量 miss）；
- 原子写（临时文件 + `os.replace`）、写前复核哈希，缩小 TOCTOU 窗口。

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
model = "deepseek-v4.1-flash"
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

- **真实模型端到端尚未在本机验证**：等待 `DEEPSEEK_API_KEY`；当前 18/18 评测使用内置 scripted provider（离线可重复）。拿到 key 后先跑 `jinlin-agent doctor`，再跑一个小仓库任务即可确认。
- 单 Agent；多 Agent 的字段与任务板只预留，不实现。
- 网络无 OS 级隔离；沙箱写限制依赖 Windows 受限令牌，构造失败会降级 L0（报告会写明）。
- 上下文压缩用确定性状态摘要，不做 LLM 摘要（更稳，但长会话信息密度略低）。
- SQLite 投影未实现（事件折叠足够 V1 规模）。
- 无 Web/IDE/GitLab 接口。

## 13. 开发

```bash
python -m pytest -q            # 单元 + 18 评测探针
python -m jinlin_agent eval --suite all
```

约定：零第三方运行时依赖；所有副作用必须落事件；工具必须返回可序列化结果；先写测试再改行为。
