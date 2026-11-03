# Jinlin Code Engineer Agent — 实施计划（v1）

> 状态：M0 已冻结。目标：可直接上手使用的生产级 code harness（CLI），不是 demo。
> 判据：arXiv:2606.10106 的 T1–T4 + 本计划的 HCS 探针 P1–P6 + 评测 E1–E8。
> 日期：2026-10-31

## 0. 决策记录（Decisions）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 语言/包 | Python 3.13，`src/jinlin_agent`，无第三方运行时依赖（stdlib only） | 本机已装 3.13.15；零依赖 = 拿到手就能跑，不受内网 pip 影响 |
| D2 | 模型 | DeepSeek OpenAI-compatible（`/chat/completions`，支持 tool calling + SSE），key 由用户后补；内置 scripted mock | 用户指定；mock 保证一切测试可离线、可重复 |
| D3 | 编辑协议 | `apply_patch`（OpenAI Codex V4A 信封，多文件原子）+ `edit_file`（old/new_string 精确唯一匹配，Anthropic 风格）共用同一安全引擎 | 行业没有唯一标准：OpenAI=apply_patch、Anthropic=Edit、Aider=SEARCH/REPLACE；双协议覆盖模型习惯，引擎统一保证安全 |
| D4 | 安全引擎规则 | 精确匹配、全有或全无、读后写哈希强制、EOL/BOM 保留、原子写（tmp+os.replace）、写前复核 | 编辑可靠性是 harness 生命线；不做模糊匹配避免静默损坏 |
| D5 | 状态真相源 | 事件日志（JSONL，append-only，seq 单调，fsync）是唯一真相；SQLite 仅作可重建投影（V1 直接用事件折叠，SQLite 留接口） | 崩溃恢复才有语义；避免双真相源 |
| D6 | 状态机 | "阶段标签（stage）+ 三条不变量"：无计划不实现、无变更不验证、无验证不完成 | 与 T1 自适应循环一致，避免装饰性状态机 |
| D7 | 上下文（T3） | 预算/阈值只做触发；进入/退出由**内容相关打分**决定（任务/计划/失败/diff/最近观察），决策入事件 | 论文 T3 可验证判据；压缩 = 用状态摘要替换历史，不额外调模型 |
| D8 | 沙箱 | 两级：L0（策略+审批+审计，诚实标注）；L1（Windows 受限令牌 WRITE_RESTRICTED + Low IL + Job Object 杀进程树）。V1 目标 L1，失败自动降级 L0 并记录真实等级 | 无 Docker/WSL；T4 的 containment 必须是真的 |
| D9 | 配置 | 内置 < 用户级 < 项目级；项目级只能收紧；端点/凭据只来自用户级与 env；effective config 哈希入事件 | 堵死仓库提权 |
| D10 | 并发 | 读类工具可并行，写类串行；结果按 tool_call_id 回填；单调用失败不打断整批 | provider 并行 tool calls 的现实语义 |
| D11 | 评测 | HCS（P1–P6）证明 T1–T4；E1–E8 证明不是玩具；指标从事件日志计算 | 论文条件是成员资格，不是质量 |
| D12 | 交付形态 | `jinlin-agent` CLI：run / resume / sessions / trace / stats / doctor / eval；非交互模式 + 退出码 + 中文报告 | "拿到手直接用" |

## 1. 架构（瘦身后的最终形态）

```
CLI (argparse)  ──  run / resume / trace / doctor / eval / sessions / stats
   │
   ├── session.py      建/开/锁 session，路径与工件目录
   ├── loop.py         Agent Loop：构造上下文→模型→解释→调度→验证→终止
   ├── context.py      内容相关选取 + 预算 + 压缩（T3）
   ├── prompt.py       Prompt 装配（稳定前缀 + provenance 包装）
   ├── model/          Provider 协议 + deepseek(OpenAI 兼容) + mock
   ├── tools/          registry + fs / patch / shell / git / task
   ├── policy/         path / command / approval（fail-closed）
   ├── sandbox/        windows L1（受限令牌+Low IL+Job Object）/ null 降级
   ├── verify.py       验证阶梯 + 基线 + Completion Gate（T4）
   ├── report.py       最终报告（claim→evidence 绑定）
   ├── events.py       事件日志（唯一真相）+ 投影
   ├── state.py        事件折叠 → SessionState
   ├── artifacts.py    大输出/快照/报告
   └── config.py       分层配置 + 项目级只紧不松
```

## 2. 事件 schema（v1，append-only）

公共字段：`schema_version, event_id, seq, ts, session_id, turn_id, step_id, type, payload`。

| 组 | 事件 | 关键 payload |
|---|---|---|
| 会话 | `session.created` / `session.resumed` / `session.checkpoint` / `session.completed` / `session.failed` / `session.cancelled` | workspace、request、config_hash、model、result、report_artifact |
| 环境 | `workspace.snapshot` / `config.escalation_blocked` / `sandbox.mode` | git 状态、命令、等级、原因 |
| 循环 | `phase.changed` / `step.started` / `context.built` / `context.compacted` / `loop.detected` | tokens、kept/dropped、signature |
| 模型 | `model.requested` / `model.attempt` / `model.responded` | request_id、attempt、finish_reason、usage、artifact |
| 工具 | `tool.intent` / `tool.completed` / `tool.failed` / `tool.denied` | call_id、args_hash、幂等键、exit_code、artifact、truncated |
| 审批 | `approval.requested` / `approval.resolved` | approval_id、风险、decision |
| 任务 | `plan.updated` / `fact.recorded` / `decision.recorded` | 版本、内容 |
| 验证 | `verification.started` / `verification.completed` | level、command、exit_code、findings |
| 完成 | `completion.proposed` / `completion.rejected` | claims、reasons |
| 测试 | `crash.injected` | point（仅测试用） |

**不变量**：`seq` 严格递增；每次 append fsync；`tool.intent` 必须先于执行；恢复时"有 intent 无 result"必须探测环境后三态处理（未生效/已生效/不可判定）。

## 3. 工具面（V1，15 个）

| 工具 | 副作用 | 说明 |
|---|---|---|
| `read_file` | READ | 行号 + 偏移/上限；记录会话内读取哈希（read-before-write 基线） |
| `list_dir` | READ | 一层目录，忽略 .git/噪声 |
| `find_files` | READ | glob，尊重 .gitignore |
| `search_text` | READ | 正则 + glob + 上下文行；上限 + artifact |
| `get_file_info` | READ | 大小/类型/哈希/行数 |
| `get_git_status` / `get_git_diff` / `get_git_log` | READ | 只读 git |
| `apply_patch` | WRITE | V4A 信封：Add/Update/Delete File，多文件原子 |
| `edit_file` | WRITE | old/new_string 精确唯一匹配 |
| `write_file` | WRITE | 仅新建/整文件重写（需审批或小文件） |
| `run_command` | EXECUTE | argv 数组、cwd、timeout、background；经沙箱 |
| `read_process_output` / `terminate_process` | READ/EXECUTE | 后台作业句柄 |
| `update_plan` | NONE | 计划即运行时状态 |
| `record_fact` | NONE | 结构化事实（来源 + 置信度） |
| `propose_completion` | NONE | 触发 Completion Gate（不可直接完成） |
| `request_user_input` | NONE | 主动求助，转 WAITING_USER |

（`delete_file` 在 V1 只允许经审批后由 `apply_patch` 的 Delete 段完成，不单列。）

## 4. 工具执行管线（唯一入口）

```
存在性 → JSON Schema 校验 → 参数规范化 → 路径策略 → 命令策略 → 审批(fail-closed)
→ tool.intent(含幂等键) → 沙箱执行 → 超时/异常 → 结果标准化
→ 超长输出 → artifact + 截断元数据 → tool.completed/failed → 回填循环
```

## 5. 上下文装配（T3）

- 固定块：身份、运行时策略、工具契约、项目指令、任务合同、当前阶段/计划、工作区状态。
- 检索块：仓库地图（缓存）、相关文件（计划/diff/错误栈/上次读取）、Working Memory（facts/decisions）、最近失败、当前 diff、最近 N 步观察。
- 打分：`score = w1*任务关键词命中 + w2*计划/diff 关联 + w3*最近失败关联 + w4*最近使用`，每条入选/丢弃都写 `context.built`。
- 预算：`capability.context_window - output_reserve`，按块最小需求分配；超出只丢可选块并记录，**绝不静默截断**。
- 压缩：soft=0.70/hard=0.85 触发；压缩 = 丢弃原始历史，用「任务 + 计划 + facts + 已改文件 + 失败原因 + 验证证据 + artifact 引用」状态摘要替代；重注入必须保留清单（§18.3）。
- 前缀稳定：固定块顺序固定、内容哈希稳定，便于 prompt cache。

## 6. 验证与完成门（T4）

- 验证阶梯：L0 diff 审查（改动范围/敏感内容/二进制）→ L1 结构（compile/parse）→ L2 静态（ruff/mypy/tsc，检测到才跑）→ L3 测试（目标→模块→全量，命令来自用户 > AGENTS.md > 项目配置 > 自动检测）→ L4 行为（项目声明才跑）→ L5 回归（基线通过的测试仍通过）。
- 基线：首次验证时跑一次，记录已失败项；区分原有失败 / 新引入失败。
- 完成门（全部为机器判定，任一不过即拒绝）：目标存在；计划步骤完成或带理由跳过；无未处理工具错误；无待审批；无冲突；diff 已检查且与最后一次验证一致；至少跑过"适当层级"（代码改动必须有 L1，有测试必须有 L3 或声明理由）；测试 exit_code=0；无无关修改；每条 claim 的 evidence 存在且一致；未发现"声称通过但证据相反"。
- 状态：`completed / completed_with_warnings / partial / blocked / failed`。

## 7. 里程碑与验收

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M0 | 计划/决策、脚手架、pyproject、config、domain/errors/tokens/artifacts、事件存储 + 状态折叠 | `python -m pytest` 通过；事件往返一致 |
| M1 | 模型适配（deepseek SSE + mock）、工具注册与执行管线、fs/patch/shell/git/task、CLI run | 真实仓库上"修一个失败测试"闭环；kill 后可恢复；P1/P2/P5 绿 |
| M2 | 策略（路径/命令/审批 fail-closed）、验证阶梯、基线、完成门、报告、配置只紧不松 | E2/E6/E7 绿；假成功 0/3；路径逃逸被阻断 |
| M3 | 上下文构造/打分/压缩、Working Memory、Prompt 装配（provenance + 预算 + 前缀哈希） | 60+ 步任务压缩 ≥2；P3a/P3b 绿 |
| M4 | Windows L1 沙箱、循环检测、重试/退避、指标、doctor、eval 套件、README | E1–E8 全绿；沙箱真实阻断越界写；文档齐全 |

## 8. 分工（Lead / Teammates，写范围不重叠）

- **Lead**：`src/jinlin_agent/**`（除 sandbox）、`tests/`（除 sandbox 测试）、`docs/`、根目录文件。
- **sandbox-win**：仅 `src/jinlin_agent/sandbox/windows.py` + `tests/test_sandbox_win.py`。
- **eval-fixtures**：仅 `fixtures/**` + `tests/test_eval_*.py`（M3 后启动）。
- **docs-writer**：仅 `docs/**` 中的用户文档（M4 启动）。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| DeepSeek 4.1-flash 对 V4A 格式不熟 | 双协议（apply_patch + edit_file）；工具描述含最小示例；失败返回精确诊断 + 重读提示；用真实模型做一次编辑成功率抽测 |
| 受限令牌/Low IL 实现失败 | 自动降级 L0 并诚实标注；沙箱模块独立可测，失败不阻塞主线 |
| 流式 tool call 拼接出错 | 累积器单元测试覆盖：分片、乱序、截断、空参、非法 JSON、并行多调用 |
| 崩溃恢复二次副作用 | intent 先落盘 + 幂等键 + 环境探测三态 |
| 上下文压缩丢关键信息 | 压缩只丢历史，保留状态摘要；重注入清单强制；P3a 探针守护 |

## 10. 验收脚本（最终交付）

```bash
python -m pytest                       # 单元 + 集成 + HCS + 安全
python -m jinlin_agent eval --suite hcs    # T1–T4 探针
python -m jinlin_agent eval --suite e1e8   # 真实任务评测
python -m jinlin_agent run --workspace ... --task "..."   # 真实使用
```

## 11. 交付状态（2026-10-31）

| 里程碑 | 状态 | 证据 |
|---|---|---|
| M0 契约冻结 | ✅ | `docs/PLAN.md`（本文件，D1–D12）、`README.md`、`AGENTS.md` |
| M1 内核纵切 | ✅ | 事件日志/状态折叠/循环/网关/工具管线/CLI；`tests/test_smoke.py` |
| M2 信任层 | ✅ | 路径/命令策略、fail-closed 审批、验证阶梯/基线/完成门、证据报告、配置只紧不松 |
| M3 上下文 | ✅ | 内容相关打分、压缩、artifact 审计、无静默截断（HCS P3/P3b） |
| M4 加固 | ✅ | Windows L1 沙箱（受限令牌 + Low IL + Job Object）、循环检测、重试、doctor、eval 套件 |
| 评测 | ✅ | `python -m pytest` 40 passed；`jinlin-agent eval --suite all` 18/18 |

已知偏差（相对初版计划）：

- 基线在会话创建时执行（而非首次验证时），以便 L5 回归比较有意义。
- SQLite 投影未实现（事件折叠直接承担 V1 查询），`state.py` 已保留投影边界。
- 完成时由 harness 自动重跑 L0/L1（有代码改动时）与 L3（项目有测试时），模型自己跑过的验证仍作为事件证据保留。
- 沙箱默认低完整性（Low IL）+ 受限令牌；若环境不允许构造，自动降级并在 `sandbox.mode` 与报告中如实标注。

## 12. v2.0 任务层（WP-V2.0-1 新增，2026-11-03）

> 依据 `docs/architecture/v2/`（差距分析 + ADR-001~010，已由负责人确认）。任务层的完整设计见该目录；本节只记录新增的事件 schema 与模块边界。

### 12.1 两套事件流的边界

- **会话事件流**（既有，`~/.jinlin-agent/sessions/<sid>/events.jsonl`）：执行证据的唯一真相，语义不变。本轮唯一的会话流变化：`report.written`（此前由 `loop.py` 发出但无折叠 handler）已收编进 `state._HANDLERS`。
- **任务事件流**（新增，`~/.jinlin-agent/tasks/<root_task_id>/events.jsonl`）：任务层的真相流，复用同一个 `EventLog`（append-only、seq 单调、fsync、尾部修复、单写者锁），事件 `session_id` 字段承载 root_task_id。**任务流事件不进 `state._HANDLERS`**——那是会话状态的折叠器；任务流的投影是 Task Ledger。

### 12.2 任务流事件类型（6 种）

| 事件 | 关键 payload | 说明 |
|---|---|---|
| `task.created` | `task`（完整 TaskSpec dict） | 根任务或子任务创建 |
| `task.status_changed` | `task_id` / `from` / `to` / `reason` | 合法迁移见 `taskmodel._TASK_TRANSITIONS`；终态不可复活 |
| `worker.registered` | `worker`（完整 WorkerRecord dict） | 一次执行尝试（attempt）登记 |
| `worker.status_changed` | `worker_id` / `task_id` / `from` / `to` / `reason` | 与任务同一张状态机表 |
| `task.message` | `message`（完整 TaskMessage 信封 dict） | 协议消息；`message_id` 唯一 = 幂等键 |
| `task.result_recorded` | `task_id` / `result`（v2 §7.4 结构化交付） | 交付校验：status/summary/tests 必填 |

### 12.3 Task Ledger（`ledger.py` + `taskmodel.py`）

- sqlite（stdlib）投影：`<home>/ledger.db`（home 派生，沿 `storage.home` 规则，无独立配置键）；WAL；`user_version` fail-closed（新版 schema 拒绝打开）。
- 写序：**先发事件、后提交行**——崩溃在中间时最多多一条可重放事件，绝不出现"行存在而日志无法解释"。
- `rebuild()`：删除并从全部任务日志重放重建投影（`jinlin-agent ledger --rebuild`）；重放历史迁移不再校验合法性（写入时已校验）；未知事件类型忽略（v2 §7.5 向前兼容）。
- 状态机：`TaskStatus`（v2 §6.3 九态，无 paused）+ 迁移表 + `SESSION_TO_TASK_STATUS` 映射（session `paused` 预留态 → task `blocked`），全部由 `tests/test_ledger.py` 锁定（ADR-003）。
- 并发（WP-1 范围）：进程内单写者（RLock）；连接不跨线程；跨进程靠 sqlite 自身锁 + 初始化锁；Worker 多线程化时由 WP-2 重新评估。

## 13. Worker Runtime（WP-V2.0-2 新增，2026-11-03）

- 模块 `worker_runtime.py`：Worker = 既有 Harness 内核的**参数化实例**（ADR-001），本模块不含第二套执行循环。
- 内核参数化（三个可选参数，默认 None，原 `run` 路径零变化）：
  - `Harness(role=RoleSpec)`：`allowed_tools` 经 `ToolRegistry.view()` 白名单过滤（白名单外工具对 dispatcher 不可达，TOOL_NOT_FOUND）；`system_instruction` 进入系统提示的 binding Role 段。
  - `Harness(budget=dict)`：per-worker 预算覆盖（max_tokens / max_tool_calls / timeout_seconds，config 兜底）；新增 `max_tool_calls` 检查。
  - `Harness(cancel_check=...)`：步边界协作式取消——进行中的模型调用/工具先完成，下一个 step 前退出（v2 §13.4 的 WP-2 形态）。
- 生命周期（v2 §6.3）：submit → queued → running →（waiting_user/approval → resume 续同一 session）→ completed/failed/cancelled；重试 = 新 Attempt（次数来自 `TaskSpec.budget.max_attempts`）；`BUDGET_EXCEEDED` 不可重试（ADR-009）。
- 并发（v2 §6.5 的 WP-2 形态）：`runtime.max_concurrent_workers`（默认 2，项目级只能减小）；**同一 workspace 整程独占**（Windows 沙箱按实例授/收工作区写 ACE，重叠实例会竞争 ACL）；不同 workspace 可并行；worktree 隔离由 WP-V2.0-5 交付（ADR-005）。
- 任务事件流新增第 7 种事件：`worker.session_bound`（worker→session 绑定；resume 依赖它，且必须可重建）。
- 交付结果：完成/失败均 `record_result`（v2 §7.4 结构）；`changed_files` 来自 `state.diff_paths()`，`tests` 来自最近 verification_runs；`commit` 字段留空，待 WP-V2.0-5 绑定。
