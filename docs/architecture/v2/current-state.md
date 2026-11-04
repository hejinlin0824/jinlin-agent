# 现状盘点（WP-V2.0-0 交付物 1）

> 本文是 v2.0 架构迁移的**真实代码基线**。所有结论均可在括号内的 `文件:行号` 处复核。
> v2.0 设计文档 §2.2 自身要求："正式开发前必须以 GitHub 当前分支代码重新核实，不得仅凭本文件假定实现仍然完全一致"——本文即该核实的结果。与设计文档不符之处见 [gap-analysis.md](gap-analysis.md) §0。

- 盘点日期：2026-11-03
- 分支：`review/followup-fixes`；盘点时检出 HEAD：`b0f3ade`（文档提交）；代码 HEAD：`17b2262`（第 10 轮整改 fix(hardening)）
- 方法：全文精读 `loop.py` / `runtime.py` / `verify.py` / `state.py` / `session.py` / `policy.py` / `paths.py` / `domain.py`；其余模块逐文件只读清点；实测 `python -m pytest -q`（junitxml 佐证）与 `python -m jinlin_agent eval --suite all`；与 `AGENT.md`（v2.7）、`README.md`、`docs/PLAN.md` 交叉核对
- 一句话结论：仓库是**单 Agent、CLI 形态、事件溯源**的代码工程 harness，内核（事件日志 / 策略 / 沙箱 / 验证 / 完成门 / 上下文）完整且经过 10 轮评审加固；**完全没有**任务层（多任务、Worker、规划编排、Web）。v2.0 设计文档 §2.2 所列 ApplicationService / Workbench / TaskContract / Web API 在代码中不存在。

---

## 1. 系统形态

- 入口只有 CLI，8 个子命令：`run` / `resume` / `sessions` / `trace` / `stats` / `doctor` / `clean` / `eval`（cli.py:353-418）
- 一次 `run` = 一个 Session = 一个 `Harness` 实例（loop.py:41）；没有第二个执行循环，也没有任何后台协调器
- 无 HTTP API、无 Web UI、无多 Agent；`clean`（trash.py）是唯一跨会话的维护命令
- 零第三方运行时依赖（pyproject.toml:13-14，`dependencies = []`），`requires-python >= 3.11`（开发环境 3.13）
- 退出码：0 completed / 1 failed / 2 waiting（user·approval·paused）/ 3 cancelled / 4 配置或致命错误（cli.py:429-433）

## 2. 模块地图（`wc -l` 实测，`src/jinlin_agent/` 共 11,291 行）

| 模块 | 行数 | 职责 |
|---|---|---|
| `sandbox/windows.py` | 1441 | Windows L1 沙箱：受限令牌（WRITE_RESTRICTED）+ Low IL 尝试 + 每命令 Job Object 杀进程树 |
| `tools/patch.py` | 1222 | 编辑引擎：apply_patch（V4A 多文件原子）/ edit_file / write_file；读后写 + 哈希守卫 + 事务回滚 |
| `evalkit.py` | 789 | 18 个可执行探针（HCS P1–P6、P3b + E1–E8），scripted provider 离线运行 |
| `verify.py` | 777 | 项目检测、baseline、验证阶梯 L0–L5、CompletionGate |
| `loop.py` | 465 | Agent Loop（Harness）：阶段推进、预算、循环守卫、完成流程 |
| `context.py` | 463 | 内容相关上下文：打分、预算、压缩、messages artifact 审计 |
| `cli.py` | 433 | argparse CLI + 交互审批 + 退出码 |
| `policy.py` | 419 | PathPolicy / assess_command / ApprovalManager（fail-closed）/ PolicyEngine |
| `state.py` | 412 | 事件折叠 → SessionState（33 个事件 handler） |
| `domain.py` | 381 | 可序列化数据模型（Message / ToolCall / TaskPlan / VerificationRun / Claim…） |
| `trash.py` | 349 | `clean` 命令：回滚残留清理（manifest + 指纹归属证明 + 隔离区验证） |
| `config.py` | 345 | 分层配置（builtin < 用户 < 项目 < env），项目级只紧不松 |
| `tools/fs.py` | 341 | read_file / list_dir / find_files / search_text / get_file_info |
| `tools/task.py` | 338 | update_plan / record_fact / record_decision / propose_completion / request_user_input |
| `runtime.py` | 332 | ToolDispatcher：唯一工具执行管线 |
| `model/openai_compat.py` | 314 | DeepSeek（OpenAI 兼容）：SSE、tool_call 累积、退避重试 |
| `events.py` | 282 | append-only 事件日志：fsync、会话锁、尾部修复、严格回放校验 |
| `session.py` | 260 | 会话创建/打开/锁/快照/恢复（孤儿 intent 三态处理） |
| `tools/shell.py` | 244 | run_command（argv、background）/ read_process_output / terminate_process |
| 其余（sandbox/null、report、prompt、sandbox/base、tools/project、tools/git、model/mock、artifacts、tools/schema、paths、errors、tools/base、locks、model/base、model/\_\_init\_\_、tools/registry、tokens、sandbox/\_\_init\_\_、\_\_init\_\_、\_\_main\_\_） | 34–189 | L0 沙箱、报告、提示装配、项目摘要、只读 git、scripted mock、artifact store、schema 校验、路径边界、错误码、工具契约、锁、Provider 协议 |

测试（`tests/`，共 2,372 行）：`test_security.py` 1405（安全/完成门/事务回归）、`test_sandbox_win.py` 405（Windows 专属，模块级平台守卫）、`test_trash.py` 368、`test_smoke.py` 163、`test_eval.py` 24（参数化 18 探针）。

## 3. 核心调用链（一次 run 的完整数据流）

```text
jinlin-agent run --workspace W -t "..."
 → cli._make_harness (cli.py:77-106)
     provider = create_provider(cfg, --mock-script?)        # DeepSeek 或 scripted mock
     approval handler: tty 交互 [y]/[a]/[N]；--yes 自动批准；非 tty 无 handler → fail-closed
 → SessionManager.create (session.py:62-104)
     session.created      （workspace/request/model/config_hash/sandbox level）
     config.escalation_blocked*（项目级越权配置被拒记录）
     sandbox.mode         （真实沙箱等级，禁止谎报）
     workspace.snapshot   （git 状态、项目类型、测试命令、既有脏文件）
     verification.baseline（任务开始前已失败的测试集合，供 L5 回归比较）
 → Harness.run (loop.py:189-207)   while not is_terminal():
     _step (loop.py:209-269)：
       context.built        （每轮重建：块分数/理由/丢弃清单，完整 messages 落 artifact）
       model.requested → provider.complete（SSE/tool-call）→ model.attempt / model.responded
       ├─ 有 tool_calls → ToolDispatcher.execute (runtime.py:83-224)
       │     存在性 → JSON 解析 → schema 校验 → PolicyEngine.evaluate
       │     → 审批（destructive 默认拒绝；ask 无应答者 fail-closed）
       │     → tool.intent（args_hash + 幂等键，先于执行落盘）
       │     → handler 执行 → 超长输出落 artifact + 截断标记
       │     → tool.completed / tool.failed / tool.denied
       │     → extra 展开：plan.updated / fact.recorded / decision.recorded / completion.proposed
       └─ propose_completion → _handle_completion (loop.py:314-365)
             _run_harness_verification（按"是否有变更 + claim 是否提到测试"选 L0/L1/L3/L5）
             → CompletionGate.check (verify.py:548-675)
             ├─ 拒绝 → completion.rejected（原因回灌历史，继续修）
             └─ 通过 → report.md + final_report artifact → session.completed
```

## 4. 事件类型（state.py:378-412，33 个有 fold handler）

`session.created` `session.resumed` `session.checkpoint` `workspace.snapshot` `phase.changed` `step.started` `context.built` `context.compacted` `model.requested` `model.attempt` `model.responded` `tool.intent` `tool.completed` `tool.failed` `tool.denied` `approval.requested` `approval.resolved` `plan.updated` `fact.recorded` `decision.recorded` `verification.started` `verification.completed` `verification.baseline` `session.waiting_user` `session.user_input` `completion.proposed` `completion.rejected` `session.completed` `session.failed` `session.cancelled` `sandbox.mode` `config.escalation_blocked` `loop.detected`

已知不一致：`report.written` 由 loop.py:462 发出但**没有** fold handler（未知类型折叠为 no-op，无害但不一致，WP-1 建事件 schema 时一并收编）。

## 5. 状态与持久化边界

- **唯一真相**：`~/.jinlin-agent/sessions/<sid>/events.jsonl`；append-only、`seq` 严格递增、每行 flush+fsync、`.lock` 会话级单写者锁（msvcrt/fcntl，locks.py:18-45）
- 状态 = 事件折叠投影（state.py `recover`/`apply_event`），可随时从空对象重建；checkpoint 每 5 步写 `session.checkpoint`（seq + state_hash）（loop.py:174-185）
- `open()` 崩溃尾部修复（events.py:58-100）：无换行 torn 碎片 → 截断到最后完整事件；完整 JSON 缺换行 → 补 `\n`；换行终止坏 JSON → fail-closed `SESSION_CORRUPTED`
- 回放校验（events.py:159-253）：空行、schema_version 不符、seq 跳号、session_id 不一致一律 `SESSION_CORRUPTED`
- 恢复（session.py:169-205）：孤儿 `tool.intent`（有 intent 无 result）三态——只读工具（白名单 6 个）安全忽略；写类标 `RECOVERY_UNCERTAIN` 提示先核对工作区
- `session.json` 仅是列表用快照（session.py:40-57），**不参与**状态判定
- artifact store：`<session_dir>/artifacts/<artifact_id>` + `index.json`（temp+`os.replace` 原子）（artifacts.py:34-100）
- 工作区写锁：`<workspace>/.jinlin/write.lock` 串行化多个 Jinlin 会话的编辑（tools/patch.py:61、trash.py:267）

## 6. 会话状态机

- `Status` 8 态（domain.py:44-55）：`created / running / waiting_approval / waiting_user / paused / failed / completed / cancelled`；终态 completed·failed·cancelled 不可复活（state.py:85-90）
- `paused` 是预留枚举：全仓只有定义（domain.py:49）和 CLI 退出码映射（cli.py:115）两处引用，**没有任何事件**把会话置为 paused
- `Phase` 9 段（domain.py:32-41）：CREATED→INITIALIZING→INVESTIGATING→PLANNING→IMPLEMENTING→VERIFYING→REPAIRING→FINALIZING→COMPLETED，由 loop.py:142-160 确定性推进
- 计划：`TaskPlan`（objective/assumptions/constraints/steps/verification_strategy/risks/version，domain.py:255-292）；步骤**平铺**，无依赖关系、无 DAG

## 7. 工具面（20 个，ToolDispatcher 是唯一入口）

| 工具 | side_effect | 用途 |
|---|---|---|
| read_file | READ | 带 provenance 头读文本（编辑前必读基线） |
| list_dir / find_files / search_text / get_file_info | READ | 目录 / glob / 正则搜索 / 元数据 |
| get_project_summary | READ | 项目类型/指令文件/布局/测试命令/git 状态 |
| apply_patch | WRITE | V4A 信封多文件原子编辑（Add/Update/Delete） |
| edit_file | WRITE | 单文件 old_string→new_string 精确唯一替换 |
| write_file | WRITE | 新建（O_CREAT\|O_EXCL）/ 整文件重写 |
| run_command | EXECUTE | argv 数组、cwd、timeout、background；经沙箱 |
| read_process_output | READ | 后台作业输出 |
| terminate_process | EXECUTE | 终止后台进程树 |
| get_git_status / get_git_diff / get_git_log | READ | 只读 git |
| update_plan / record_fact / record_decision | NONE | 计划与工作记忆（结构化，落事件） |
| propose_completion | NONE | 触发完成门（模型不能直接完成） |
| request_user_input | NONE | 暂停等用户（session.waiting_user） |

注意：**没有任何工具声明** `SideEffect.NETWORK / DESTRUCTIVE / EXTERNAL_SIDE_EFFECT`（grep 无命中）；网络/删除风险目前只出现在 `run_command` 的命令级风险评估里（policy.py:99-100、143-147）。这对 v2 的能力模型（§9.4）是关键事实。

## 8. 策略与安全边界（已实现，全部在代码中强制）

- **路径**：所有文件入口走 `paths.resolve_in_workspace`（paths.py:58-86）：词法边界 → realpath 边界（symlink + Windows Junction/reparse，叶子不存在也拦）→ 拒绝 link 组件。受保护路径：`.env*`、密钥、`credentials.*`、`.git/**` 等（policy.py:29-45）
- **命令**：argv 数组，不做 `shell=True`；shell 包装器与 `python -c` 直接 HIGH+destructive（policy.py:133-136）；风险分级 low/medium/high/destructive + network 标志（policy.py:125-178）
- **审批**：destructive 默认拒绝；`ask` 无应答者 → 拒绝（fail-closed，policy.py:241-242）；session 级授权按类别
- **配置分层**：builtin < 用户级 < 项目级（只紧不松），规则表 config.py:126-155：模型/沙箱/存储键项目级禁改；`allow_write/allow_delete` 只能置 false；审批键只能更严；预算键只能减小；违规记 `config.escalation_blocked` 并入事件
- **沙箱**：L1 = `CreateRestrictedToken(WRITE_RESTRICTED)`（限制 SID = logon SID + Everyone）+ workspace 可继承写 ACE + 每命令独立 Job Object（KILL_ON_JOB_CLOSE，超时/关闭杀整棵树）（sandbox/windows.py:1-42 头注）；Low IL 尝试失败回退 medium 并如实写 `details`；构造失败降级 `L1-job` / L0 并写 `downgrade_reason`。L0 = 纯 subprocess + best-effort 杀树（sandbox/null.py）
- **注入**：仓库内容一律 `<untrusted source=... sha256=...>` 包装（prompt.py:116-130）；权限由代码判定，提示词无法提权
- 子进程环境：强制 `PYTHONUTF8=1`、TMP/TEMP/TMPDIR 重定向 `<workspace>/.jinlin/tmp`、默认 `-p no:cacheprovider`（sandbox/base.py:102-128）

## 9. 验证阶梯与完成门（已实现）

- **基线**：会话创建时跑一次项目测试，记录"任务开始前已失败"的集合（session.py:107-143）
- **阶梯**（verify.py:264-504）：
  - L0 diff 审查：改动文件、敏感内容模式、超大文件、git diff stat
  - L1 结构：改动文件 compile / JSON / TOML 解析
  - L2 静态：检测到 ruff/mypy/eslint 等才跑
  - L3 测试：项目测试命令（支持 pytest/npm/cargo/go/make），解析 passed/failed/FAILED·ERROR nodeid
  - L5 回归：与基线失败集合比较；timeout、无 exit_code、未知非零退出、exit0 但输出含失败 → 全部 fail（fail-closed）
  - **L4（行为验证）已声明未实现**：`VerificationLevel.L4_BEHAVIOR` 枚举存在（domain.py:97），`run_level` 无对应分支 → 落到 "no implementation for level L4" → skipped（verify.py:275-285）
- **完成门** 13 类机器判定拒绝条件（verify.py:548-675）：无任务目标；计划无效/有未完成步骤/完成步骤无证据；最近工具失败未解决；待审批；未解决编辑冲突；存在未归属的工作区改动（与 agent 自报改动 + 既有脏文件差集）；有代码改动但 L0/L1/L3 缺失或失败；L5 新失败；claim 无证据或引用不存在的证据；测试类 claim 必须引用**通过的** L3（ver_* id / artifact_* / "L3 tests" / 测试命令别名）
- 结果分级：`completed / completed_with_warnings / partial / blocked / failed`（domain.py:101-106）

## 10. 上下文工程（T3，已实现）

- 每轮重建而非追加；固定块 `task` `user_inputs` `plan` `status`（required，永不丢）+ 可打分块 `facts` `diff` `verification` `failure` `file:<path>`（context.py:176-255、348-410）
- 预算 = `max(2048, context_window − output_reserve(8192))`；可选块按分数入，另有 `max_retrieval_tokens`(40000) 双重限制；被丢块全部记录理由（context.py:97-130）
- 压缩阈值 soft 0.70 / hard 0.85：丢历史，用「末 8 步消息 + 任务/计划/facts/改动/失败/验证/artifact 引用」摘要替换；模型报 overflow 时强制压缩（context.py:414-447、loop.py:235-237）
- 审计：每次构造写 `context_messages` artifact；`context.built` 记录每块的分数与去留

## 11. 模型接入

- `Provider` 协议：`complete(request, on_text_delta) -> ModelResponse`，同步（model/base.py:55-60）
- DeepSeek 走 OpenAI 兼容 `/chat/completions`：SSE 解析、tool_call 按 index 累积、`finish_reason=length` 下坏 JSON 保留 parse_error 不崩溃、429/5xx/超时指数退避重试（max_retries=3）（model/openai_compat.py）
- `MockProvider`：脚本化步骤（text / tool_calls / raise / raw_arguments / callable），记录全部请求——18 个离线探针的基座（model/mock.py）

## 12. 测试与评测基线（本机实测 2026-11-03）

见 [test-baseline.md](test-baseline.md)。要点：`python -m pytest -q` = **105 passed / 0 failed / 0 skipped**（junitxml 佐证）；`python -m jinlin_agent eval --suite all` = **18/18**；CI = windows-latest + ubuntu-latest × Python 3.13（.github/workflows/ci.yml:12-16），第 10 轮整改 run 41306414718 双平台绿（AGENT.md frontmatter）。

## 13. Windows 环境事实

- Windows 11、Python 3.13、**无 Docker、无 WSL**、Git 可用（AGENT.md §6.1）
- L1 沙箱在本机实测可用：workspace 内写成功；`C:\`、`C:\Windows\Temp`、用户 profile 写入均 `PermissionError`（README §5；tests/test_sandbox_win.py）
- 已踩平的平台坑：cp936 编码（强制 UTF-8）、`.cmd` shim（拒绝或解析为真实入口）、pytest9+受限令牌的 cache 目录、同秒 stale `.pyc`（AGENT.md §9）

## 14. 对 v2.0 的复用评估（概览）

| v2 蓝图组件 | 本仓库对应物 | 状态 |
|---|---|---|
| 统一 Agent Loop | `loop.py` Harness | ✅ 可直接复用（Worker = 该内核的受控实例） |
| 事件溯源真相源 | `events.py` + `state.py` | ✅ 会话级完备；任务级账本缺失 |
| Tool Registry / Policy Engine | `runtime.py` + `policy.py` | ✅ 已有；缺"任务级能力集/路径范围"维度 |
| Sandbox | `sandbox/`（L1 Windows / L0） | ✅ shell 层已实测；网络无防火墙级隔离（如实声明） |
| Verification / Completion Gate | `verify.py` | ✅ 会话级完备；缺子任务级与集成级 |
| Context Provider | `context.py` | ✅ 打分/预算/压缩已实现；缺 Worker 级装配接口 |
| Task Ledger / TaskSpec / RoleSpec | — | ❌ 完全缺失（WP-1） |
| Runtime Scheduler / Worker 生命周期 | — | ❌ 完全缺失（WP-2） |
| Planner / DAG / 复杂度评估 | 仅平铺 `update_plan` | ❌ 缺失（WP-3） |
| Task Protocol（worker 间消息） | — | ❌ 缺失（WP-4） |
| Worktree / Integration Coordinator | — | ❌ 缺失（WP-5） |
| Web API / Workbench UI | — | ❌ 缺失（WP-9） |

逐项差距、证据与实施建议见 [gap-analysis.md](gap-analysis.md)。
