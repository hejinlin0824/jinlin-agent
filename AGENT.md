---
handoff_document: true
project: jinlin-agent (Python code-engineering agent harness)
version: 2.2
generated_at: "2026-11-01T14:30+08:00"
repository: https://github.com/hejinlin0824/jinlin-agent
working_directory: "E:\\codebase\\玩耍项目\\jinlin-agent"
branches:
  main: 85854f1   # 受保护基线，评审要求不得直接改
  review/followup-fixes: f1b0cb8+   # 第 4 轮整改代码提交；docs 提交后 HEAD 再 +N
ci: .github/workflows/ci.yml  # windows-latest + ubuntu-latest, Python 3.13
test_status: "python -m pytest -q => 76 passed; jinlin-agent eval --suite all => 18/18"
review_status: "第 4 轮 P1×2（恢复路径 no-clobber）已修 + P2-1 严格 UTF-8 已补；P2-2/P2-3 单独排期；等待最后一轮复核"
primary_goal: "让每一个『完成』都由真实修改 + 验证记录 + 可复查证据决定"
non_goals: [multi-agent, IDE/Web UI, network isolation, auto-git-commit]
---

# AGENT.md — 项目交接与操作手册（给接手 AI）

> **给接手者的第一条指令**：先完整读完本文件，再读 §3 的评审结论，然后按 §8 的流程工作。
> 如果只允许记一条：**不要直接改 `main`；修复必须在 `review/followup-fixes` 分支上、先写失败测试、再改实现、跑全量 pytest + offline eval、CI 双平台绿，再交给所有者合并。**
>
> 本文件由上一任执行 Agent 撰写，目标是把"项目背景 + 已做的工作 + 当前状态 + 操作细节 + 待办与踩坑"一次性交给下一个 AI，使其**不需要重新探索**就能接手。

---

## 1. 项目是什么

### 1.1 一句话

`jinlin-agent` 是一个**从零实现的、Python 写的单 Agent 代码工程 harness（CLI）**：它把"给仓库派一个工程任务"变成一段有运行状态、有权限边界、有事件记录、有验证证据、可中断恢复的工程执行过程。

它**不是**：通用 Agent 平台、IDE、多 Agent 公司、SWE-bench 榜单系统、生产级安全沙箱。

### 1.2 背景与目标来源

1. **设计规格**：项目最初依据一份 2399 行的中文规格书（2026-10-31），核心是"Event-Driven Stateful Agent Harness"。
2. **学术判据**：目标是满足 arXiv:2606.10106 提出的 agent harness 必要充分条件（T1–T4）：
   - **T1**：交错推理/行动/观察的运行时循环；
   - **T2**：让模型**改变**外部环境的工具接口（只读不算）；
   - **T3**：上下文进出决策必须依赖**任务/观察内容**（不是按 buffer 大小机械截断）；
   - **T4**：**不依赖模型配合**的控制机制（上限、验证、确定性动作）。
   论文明确：成员资格二值、质量渐进；多 Agent/UI/微调**不是**必要条件。因此"通过四条件"不等于"成熟产品"，本项目用 18 个可执行探针同时证明"是 harness"和"不是玩具"。
3. **产品目标**：任务可执行、过程可恢复、结果可验证、风险可控制、过程可审计、模型可替换、界面可扩展（V1 只有 CLI）。
4. **V1 明确不做**：多 Agent、IDE、自动 commit/push、生产凭据、浏览器自动化、K8s/云调度。

### 1.3 判断"做对了没有"的标准

- 完成门能否拒绝"假成功"（模型未修好却声称测试通过）；
- 事件日志能否在崩溃后恢复且不猜副作用是否发生；
- 编辑是否读后写、精确匹配、哈希守卫；
- 上下文是否按内容选取、压缩不丢关键信息、不静默截断；
- 沙箱等级是否**如实上报**（L1 有实测证据，失败降 L0 并写明）；
- 是否零第三方运行时依赖、可离线安装；
- 所有"已完成"是否有可复查证据（ver_* / artifact_* / 文件路径）。

---

## 2. 当前仓库状态（2026-11-01，第 3 轮整改后）

### 2.1 分支与提交

| 分支 | HEAD | 说明 |
|---|---|---|
| `main` | `85854f1` | 受保护基线；评审明确要求**不要**直接改 |
| `review/followup-fixes` | `f1b0cb8`+ | 第 4 轮整改代码提交；其后是 docs 提交，推前用 `git log --oneline -3` 确认 |
| `origin/main` | `85854f1` | 与本地一致 |

代码基线（被第 3 轮复核审阅的代码提交）：`9a935d4`。第 3 轮整改代码提交：`22d4c7d`（O_EXCL 创建 / workspace 写锁 / 先移动后校验回滚 / EventLog 尾部修复）。第 4 轮整改代码提交：`f1b0cb8`（统一 `_restore_no_clobber` 放回 / 删除覆盖式 fallback / 严格 UTF-8）。仓库已迁到 `E:\codebase\玩耍项目\jinlin-agent`（原 `C:\Users\jinlin.he\Documents\deepseek-harness\default-workspace\jinlin-code-agent` 不再存在）。

### 2.2 验证状态

```bash
python -m pytest -q            # 76 passed（42 基础 + 34 安全/完成门/事务回归）
jinlin-agent eval --suite all  # 18/18 probes passed（离线，scripted provider）
```

CI：`.github/workflows/ci.yml`，矩阵 `windows-latest` / `ubuntu-latest`，Python 3.13；跑 pytest（junit.xml）+ 离线 eval + artifact 上传。
上一轮已核实的成功 run（代码提交 `9a935d4`）：<https://github.com/hejinlin0824/jinlin-agent/actions/runs/41270143374>
本轮 `22d4c7d` 的 run 见分支 Actions 页；`main` 尚未合并，因此 main 上可能没有 CI 成功记录。

### 2.3 评审历史（三轮）

| 轮次 | 文件（外部，由所有者提供） | 结论 | 处理 |
|---|---|---|---|
| 第 1 轮 | `JINLIN_AGENT_REVIEW.md` | 实验型 Harness 7.0/10；生产级 4.5/10；P0×2、P1×6、P2×4 | 已修 P0×2 + P1×6 + P2×2，见 `docs/REVIEW-RESPONSE.md` |
| 第 2 轮 | `JINLIN_AGENT_REVIEW_FOLLOWUP.md` | 接受大部分；要求修：回滚并发、L5 `exit_code=None`、事件尾判定 | 已修，见 `docs/REVIEW-RESPONSE.md` §0 |
| **第 3 轮** | `JINLIN_AGENT_SUBMISSION_REVIEW.md` | **Request changes / 暂缓合并**：L5 通过；**2 个 P1 待修** | **两个 P1 已修**（`22d4c7d`，见 §3 与 `docs/REVIEW-SUBMISSION-ROUND3.md`） |
| **第 4 轮（当前）** | 《第 3 轮整改复核意见》 | **Request changes**：上轮全过；**P1×2 恢复路径残留竞争** + P2×3 | **P1×2 已修 + P2-1 已补**（`f1b0cb8`，见 `docs/REVIEW-SUBMISSION-ROUND4.md`）；P2-2/P2-3 单独排期；等待最后一轮复核 |

⚠️ 三个评审报告目前都在所有者本机 `C:\Users\jinlin.he\Downloads\` 下，**不在仓库里**。如果接手环境读不到，请让所有者粘贴或提供文件；本文件 §3 已完整摘录第 3 轮的要求。

---

## 3. 第 3 轮评审的两个 P1（**已修复**，保留本节作背景）

> 以下是复核报告的原文要求 + 落地解读。两项已于提交 `22d4c7d` 修复（TDD：先写失败测试看红，再实现转绿），实现与测试不得回退。若下一轮复核提出新问题，按 §8 流程处理。

### 3.1 P1-A：`create=True` 不是原子 create-only；`expected_sha256` 仍有 TOCTOU

**位置**：`src/jinlin_agent/tools/patch.py`，`atomic_write()`（约 85–128 行）与 `_rollback_transaction()`（约 151–193 行）。

**问题**：

1. `atomic_write(create=True)` 的顺序是 `if create and path.exists(): raise` → `os.replace(tmp, path)`，两步不是原子的。外部进程可在检查后、replace 前创建目标，随后被 `os.replace` **覆盖**。
2. `expected_sha256` 路径：读目标 hash → `os.replace`；hash 检查与替换之间仍有窗口，外部修改会被覆盖。
3. `_rollback_transaction()` 同样存在 check-then-act：Add 先 hash 再 `unlink`；Update 先 hash 再 `atomic_write`；Delete 先 hash 再 `os.replace(backup, path)`。
4. 现有测试的竞争注入点**早于** `atomic_write` 内部最后检查，没覆盖真正窗口。

**复核给出的修复方向**（可采纳，但设计由你负责）：

- Add：用文件系统原子的 no-clobber 建立方式，而不是 `exists()` + `os.replace`。可行方案：`os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)` 直接独占创建 + 写入 + `fsync` + `close`（Windows/POSIX 均支持 `O_EXCL`）；若目标已存在则 `FileExistsError`，映射为 `PATCH_CONFLICT`。这样不再需要 replace。
- Update：跨平台普通文件 API **无法**对任意外部进程提供"按内容 hash 的原子 compare-and-swap"。复核允许收缩承诺：
  - 明确文档/`docstring`：写前 hash 复核只是**缩小** TOCTOU 窗口，不是消除；
  - 用 workspace 级锁（例如 `<workspace>/.jinlin/lock` 的 advisory lock）串行化**多个 Jinlin 会话**之间的写；
  - 把"无法防止任意外部进程在最后窗口内修改"写进安全模型（`docs/REPORT-AND-HANDBOOK.md` §5/§16 与 `AGENT.md`）。
  - 仍要保留写后校验；回滚失败必须 `RECOVERY_UNCERTAIN`。
- 回滚：同样收缩为"best-effort ownership check"，并为"检查之后、unlink/replace 之前被外部改"补注入测试；无法严格时明确报告。

**复核要求的回归测试（必须先写、先看它失败）**：

1. `test_apply_patch_add_no_clobber_at_replace_window`：monkeypatch `patch_mod.os.replace`，在真实 `os.replace(tmp, target)` **之前瞬间**创建 `target`（内容 `external`）；断言外部文件不被覆盖。当前实现预计失败。
2. `test_apply_patch_update_toctou_after_hash_check`：monkeypatch `patch_mod.os.replace`，当目的路径是被测目标时，先把目标改为 `external-edit-after-check`，再调用真实 `os.replace`；断言不会静默覆盖外部改动（或明确断言"已收缩承诺 + 锁保护"的新行为）。
3. `test_rollback_after_check_does_not_delete_external`：在 rollback 的 hash 检查之后、unlink/replace 之前改文件，断言外部内容保留 / 报告 `RECOVERY_UNCERTAIN`。

### 3.2 P1-B：`EventLog.open()` 容忍 torn tail，但没有修复尾部就追加

**位置**：`src/jinlin_agent/events.py`，`EventLog.open()`（约 101–110 行）与 `_iter_events_checked()`（约 162 行起）。

**问题**（第 3 轮最重要的新发现）：

- 读取器允许"末尾无换行的不完整 JSON"作为 torn tail 忽略；
- 但 `EventLog.open()` 只是扫描得到 `_last_seq`，随后以 `open(path, "ab")` 追加；
- 结果是新事件直接拼在 `{"seq":` 后面，形成"换行终止的坏 JSON"，下次读取永久 `SESSION_CORRUPTED`；
- 变体：最后一条 JSON 完整但缺结尾换行，append 会把两条 JSON 拼在同一行；
- `open()` 解析失败时还要保证释放已取得的 session lock。

**修复方向**：

1. 取得写锁后，读取整个文件 bytes，扫描出"最后一个完整、可接受事件"的 byte offset；
2. 若末尾是**无法解析且无换行**的 fragment → 截断到该 offset，`flush + fsync`；
3. 若最后一条 JSON **有效但无结尾换行** → 追加 `\n`，`flush + fsync`；
4. 若末尾是**换行终止的坏 JSON** → 仍然 fail closed（`SESSION_CORRUPTED`），不自动修复；
5. 完成修复后再以 append 模式打开；
6. `open()` 抛异常时必须释放 lock（try/except/finally 或先记录 lock 句柄再清理）。

**复核要求的回归测试**：

1. `test_open_repairs_torn_tail_before_append`：完整 event + 无换行 `{"seq":` → `EventLog.open()` → append 一个事件 → close → `read_events()` 得到 `[1,2]`，文件里无 torn fragment。
2. `test_open_repairs_missing_trailing_newline`：一条有效 JSON 无 `\n` → open → append → close → 读出两条独立事件（不是拼接行）。
3. `test_open_failure_releases_session_lock`：写入换行终止的坏 JSON → `EventLog.open()` 抛 `SESSION_CORRUPTED` 后，第二个 `EventLog` 实例能立即取得锁。

### 3.3 复核建议但**不是**必须（不阻塞合并）

- L5 边界：基线已有失败 ID、本次输出同时包含该已知失败和一个未解析成 ID 的失败摘要时，是否应比较失败计数？需结合测试框架输出格式，复核标注"不是首要问题"。
- CI 增加 Python 3.11 / 3.12 矩阵（项目声明 `>=3.11`，当前只测 3.13）。
- 跨文件崩溃事务、递归脱敏、最终 token budget、30–50 真实任务集继续延后（见 §10）。

---

## 4. 架构与数据流

### 4.1 目录与模块地图（`src/jinlin_agent/`）

| 模块 | 行数（约） | 职责 |
|---|---|---|
| `cli.py` | 333 | argparse CLI：run/resume/sessions/trace/stats/doctor/eval |
| `loop.py` | 435 | Agent Loop、阶段推进、完成流程、循环/预算守卫 |
| `runtime.py` | 296 | 唯一工具执行管线（schema→policy→approval→intent→execute→artifact→event） |
| `context.py` | 418 | 内容相关上下文打分、预算、压缩、messages 组装 |
| `prompt.py` | 114 | 系统提示装配 + `<untrusted>` 包装 |
| `verify.py` | 697 | 项目检测、baseline、验证阶梯 L0–L5、CompletionGate |
| `policy.py` | 362 | 路径/命令策略、审批（fail-closed） |
| `paths.py` | 67 | 唯一可信路径解析（词法+realpath+Junction+link 组件） |
| `sandbox/base.py` `null.py` `windows.py` | 98/169/1286 | 沙箱契约、L0、Windows L1（受限令牌+Job Object） |
| `events.py` | 约 260 | append-only 事件日志 + 严格回放校验 + open() 崩溃尾部修复 |
| `locks.py` | 71 | 跨平台顾问文件锁（SessionLock：会话单写者 / workspace 写锁） |
| `state.py` | 319 | 事件折叠 → SessionState（33 种事件） |
| `session.py` | 242 | 会话创建/锁/快照/恢复/baseline |
| `artifacts.py` | 83 | artifact store（大输出、报告、快照） |
| `report.py` | 127 | 证据绑定最终报告 |
| `config.py` | 306 | 分层配置（项目级只紧不松） |
| `domain.py` | 299 | 可序列化数据模型（Message/ToolCall/Plan/Fact/Verification/Claim…） |
| `errors.py` | 65 | 稳定错误码 |
| `tokens.py` | 32 | 启发式 token 估算 |
| `evalkit.py` | 697 | 18 个可执行探针（HCS P1–P6 + E1–E8） |
| `model/` | base 45 / mock 93 / openai_compat 285 | Provider 协议、scripted mock、DeepSeek OpenAI 兼容（SSE/tool-call 累积/重试） |
| `tools/` | fs 304 / patch 702 / shell 223 / git 91 / task 321 / project 101 / schema 75 / registry 38 / base 61 | 20 个工具 + 执行上下文 |

测试：`tests/test_smoke.py`(127)、`test_security.py`(353)、`test_eval.py`(16，参数化 18 探针)、`test_sandbox_win.py`(331)。

### 4.2 一次会话的数据流

```
用户任务
 → session.created（workspace/request/model/config_hash/sandbox level）
 → workspace.snapshot（git 状态、项目类型、测试命令、既有脏文件）
 → baseline 测试（记录"任务开始前就失败"的测试）
 → loop.step × N：
     context.built（块分数/理由/丢弃）
     → model.requested/attempt/responded
     → tool.intent → tool.completed/failed/denied
     → plan.updated / fact.recorded / decision.recorded
     → verification.*（模型自己跑的测试也算证据）
 → propose_completion
     → harness 自动跑 L0/L1/L2/L3/L5
     → CompletionGate（机器判定，见 §5.5）
         ├─ 拒绝 → completion.rejected（原因回灌，继续修）
         └─ 通过 → report（claim→evidence）→ session.completed
```

### 4.3 模块依赖（禁止反向依赖）

```
cli → loop → {context, prompt, verify, runtime, model, session, policy}
runtime → {tools, policy, artifacts, events}
verify → {sandbox, artifacts, domain, paths}
policy → paths
tools/* → {base, schema, registry, locks}
sandbox/* → base
events → {domain, errors, locks}
locks → ∅（最底层，供 events 与 tools 共用）
state → domain
```

---

## 5. 关键机制（改代码前必须理解）

### 5.1 事件日志（唯一真相）

- 文件：`~/.jinlin-agent/sessions/<session_id>/events.jsonl`（每行一个 JSON，`seq` 从 1 递增）。
- 写入：`EventLog.append` 单行写入 + `flush + os.fsync`；`open()` 取 `.lock`（msvcrt/fcntl）单写者。
- 事件公共字段：`schema_version=1, event_id, seq, ts, session_id, turn_id, step_id, type, payload`。
- 33 种事件类型见 `state._HANDLERS`（`session.*/workspace.snapshot/phase.changed/step.started/context.*/model.*/tool.*/approval.*/plan.updated/fact.recorded/decision.recorded/verification.*/completion.*/sandbox.mode/config.escalation_blocked/loop.detected`）。
- 读取校验（`_iter_events_checked`）：只容忍**末尾无换行**的 torn fragment；换行终止坏 JSON、空行、schema/session 不一致、seq 缺口 → `SESSION_CORRUPTED`。
- **恢复**：`SessionManager.open` 折叠事件 → 检测"有 intent 无 result"的工具调用 → 只读安全忽略；写类标 `RECOVERY_UNCERTAIN`；`EventLog.open` 取锁后**先修复尾部再追加**（无换行坏碎片截断到最后完整事件；完整缺换行补 `\n`；换行坏 JSON 仍 fail-closed；失败释放锁，见 §3.2，已修复）。
- **改动约束**：新增事件类型必须同时加到 `state._HANDLERS`、必要时文档描述、以及探针/测试。

### 5.2 路径边界（唯一入口）

- `paths.resolve_in_workspace(workspace, raw, follow_symlinks)`：
  1. 词法边界（`..`/绝对路径）；
  2. realpath 边界（symlink + Windows Junction/reparse，**叶子不存在也拦**）；
  3. `follow_symlinks=false` 时逐级拒绝 link 组件。
- 所有文件入口必须走它：`ToolContext.resolve()`、`PathPolicy.resolve()`、补丁、Shell cwd。
- 受保护路径：`.env*`、`*.pem/key/p12/pfx`、`id_rsa/ed25519`、`credentials.*`、`secrets.*`、`.git/**`。
- 已知边界：路径解析不是针对恶意并发替换目录项的无竞态沙箱；完全消除需要目录句柄/平台 API（文档已声明）。

### 5.3 编辑引擎（`tools/patch.py`）

- 两种协议，共用安全引擎：
  - `apply_patch`：V4A 信封，多文件、全有或全无；
  - `edit_file`：精确 `old_string` → `new_string`，唯一匹配；
  - `write_file`：新文件 `create=True`；已存在文件强制读后写 + 哈希。
- 规则：精确匹配（无模糊）、读后写、写前 hash 复核、写后校验、原子写（tmp + `os.replace`）、保留 BOM/CRLF、成功后清理该 `.py` 的 `__pycache__`（同秒 stale pyc 修复）。
- 事务：写入成功后才登记回滚记录（含写后 hash）；回滚只回滚"仍与本事务写后 hash 一致"的路径，否则 `RECOVERY_UNCERTAIN` + `rollback_incomplete`；Delete 用原子重命名备份。
- **并发边界（第 3 轮整改后）**：新建文件 = `O_CREAT|O_EXCL` 独占创建（文件系统级原子 no-clobber，不再用 `os.replace`）；更新 = 写前哈希复核**缩小**（不消除）TOCTOU + workspace 写锁（`<workspace>/.jinlin/write.lock`）串行化 Jinlin 会话（`WORKSPACE_LOCKED` 可重试）；回滚 = 先移动到事务级 trash 再校验归属，Delete 恢复用 `os.link`；对外部进程的残余窗口如实写入 `docs/REPORT-AND-HANDBOOK.md` §16。

### 5.4 上下文（T3）

- 每轮重建，不无限追加；required 块（任务/计划/状态/用户回答）优先，检索块按任务关键词+计划+失败+改动打分。
- 预算 = `context_window - output_reserve`；超出只丢可选块并写 `dropped[].reason`；**绝不静默截断**。
- 压缩（soft 0.70 / hard 0.85）= 丢历史，用「任务+计划+facts+改动+失败+验证证据+artifact 引用」的确定摘要替换（不额外调模型）。
- 每次构造把完整 messages 写 `context_messages` artifact，`context.built` 记录块分数/理由/丢弃。
- **已知未闭环**：最终 messages 的二次统一估算 / required 超预算的 `BUDGET_EXCEEDED`（§10）。

### 5.5 验证阶梯与完成门（T4）

- L0 diff 审查（敏感内容/大文件/diff stat）→ L1 结构（compile/JSON/TOML）→ L2 静态（检测到 ruff/mypy 才跑）→ L3 测试（项目命令，解析 passed/failed 与 **FAILED/ERROR nodeid**）→ L5 回归（与 baseline 失败集合比较；timeout/无退出码/未知非零/exit0 含失败全部 fail）。
- 完成门（`CompletionGate.check`）在以下情况拒绝：
  1. 无任务目标；2. 计划未完成/无证据/未解释跳过；3. 最近工具失败未解决；4. 待审批；5. 未解决编辑冲突；
  6. 未归属工作区改动；7. 有代码改动但 L0 未跑/失败；8. 有代码改动但 L1 未跑/失败；9. 项目有测试但 L3 未跑/失败；10. L5 新失败；
  11. claim 证据不存在；12. 测试类 claim 未引用**通过的 L3**；13. claim 说"测试通过"但最新 L3 失败。
- 结果分级 `completed / completed_with_warnings / partial / blocked / failed`，写进报告。

### 5.6 沙箱

- `L1`（Windows）：`CreateRestrictedToken(WRITE_RESTRICTED)`（restricting SIDs = logon SID + Everyone）+ workspace 可继承写 ACE + `CreateProcessAsUserW`；每个命令一个 Job Object（`KILL_ON_JOB_CLOSE`），`CREATE_SUSPENDED → Assign → Resume`，超时/close `TerminateJobObject` 杀整树。
- Low IL 尝试后由真实写探针否决（workspace 是 Medium 标签）→ 回退 medium integrity 并写 `details`；**不重标用户 workspace**。
- 失败自动降级 `L1-job/job-object` 或 L0，并在 `sandbox.mode` 事件与报告中如实写明；**禁止谎报等级**。
- 子进程 env：`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8:replace`、`TMP/TEMP/TMPDIR=<workspace>/.jinlin/tmp`；未设置 `PYTEST_ADDOPTS` 时注入 `-p no:cacheprovider`（避免受限令牌下 pytest cache 目录问题）。

### 5.7 模型适配

- `model/base.py`：`Capabilities`（context_window/max_output/tools/streaming）、`ModelRequest`、`Provider.complete`。
- `model/openai_compat.py`：DeepSeek `/chat/completions`；SSE 解析；tool_call delta 按 index 累积；`finish_reason=length` 下非法 JSON 保留 `parse_error` 不崩溃；429/5xx/超时按退避重试；`reasoning_effort` 透传。
- 真实模型：**API id 是 `deepseek-flash`**（`deepseek-chat`/`deepseek-reasoner` 是别名），context 1,048,576，默认 `temperature=0`、`reasoning_effort=high`。
- 测试用 `model/mock.py`：脚本化步骤（text/tool_calls/raise/raw_arguments），记录全部请求（探针用它断言上下文）。

### 5.8 评测（`evalkit.py` + `tests/test_eval.py`）

- HCS 探针：P1 自适应循环、P2 改变环境、P3 内容相关上下文+压缩、P3b 无静默截断、P4a 假成功、P4b 路径逃逸、P4c 循环、P4d fail-closed 审批、P5 崩溃恢复、P6 证据绑定。
- E1–E8：只读分析、小型修复、跨文件修改、新文件+测试、恢复、权限、假成功、循环。
- 每个探针 = 临时 fixture 仓库 + scripted provider + 事件断言；离线、确定性。
- 运行：`jinlin-agent eval --suite all`（注意：**不要**带 `--workspace` 指向仓库根，否则会在当前目录生成 fixture；默认用临时目录）。

---

## 6. 操作手册

### 6.1 安装与自检

```bash
cd jinlin-code-agent
python -m pip install -e .
python -m pip install pytest
jinlin-agent --version          # 0.1.0
jinlin-agent doctor             # model/sandbox/project 自检
```

运行环境事实（所有者机器）：Windows 11、Python 3.13.15（`pyproject` 声明 >=3.11）、无 Docker/WSL、Git 可用。**零第三方运行时依赖**（`pyproject.toml` 的 `dependencies = []`）。

### 6.2 真实使用

```bash
export DEEPSEEK_API_KEY=...     # 只放环境变量；不要写入任何文件
jinlin-agent run --workspace /path/to/repo -t "修复失败测试"
jinlin-agent run --workspace /path/to/repo --read-only -t "梳理入口和调用链"
jinlin-agent run --workspace /path/to/repo --yes -t "..."     # 非交互；破坏性仍拒绝
jinlin-agent resume <session_id> [--answer "..."]
jinlin-agent trace <session_id> --tail 80
jinlin-agent stats <session_id>
```

退出码：0 完成 / 1 失败 / 2 等待用户或审批 / 3 中断 / 4 配置错误。

配置：`~/.jinlin-agent/config.toml`（用户级）；`<repo>/.jinlin/config.toml`（项目级，**只能收紧**）；环境变量 `JINLIN_MODEL/JINLIN_BASE_URL/JINLIN_API_KEY_ENV/JINLIN_HOME`。示例见 `config.example.toml`。

### 6.3 开发与验证

```bash
python -m pytest -q                    # 必须 64 passed
jinlin-agent eval --suite all          # 必须 18/18
```

CI：推送 `main` / `review/**` / `dev` 或 PR 时触发；Windows + Ubuntu，Python 3.13；产物 `reports/junit.xml`、`reports/eval.txt`。

### 6.4 Git 与分支流程（重要）

- `main` = `85854f1`，**受保护**；评审要求不得直接改。
- 所有修复提交到 `review/followup-fixes`（或从它切出的新分支），push 后由所有者/评审确认再合并。
- 合并（评审确认后，由所有者执行）：

```bash
git checkout main
git merge --ff-only review/followup-fixes
git push origin main
```

- 推送使用 Windows Credential Manager 中的 GitHub 凭据（无需输入 token）；`~/.gitconfig` 配置了 `http.https://github.com/.proxy=http://127.0.0.1:7890`，代理不可用时 push 会报 `Connection was reset`，先确认 127.0.0.1:7890 可连。
- 提交信息用英文、语义化（`fix(review): ...`、`test: ...`、`docs: ...`、`ci: ...`）。

### 6.5 凭据与安全

- DeepSeek key 只从环境变量读；事件/artifact/报告不含明文（已 grep 验证）。
- **历史 key 已暴露过，所有者应轮换**；接手者不要复用旧 key，不要写入仓库。
- 仓库里若发现 `sk-` 明文，视为事故：立刻报告所有者并清理历史。

---

## 7. 修改代码的硬规则

继承 `AGENTS.md` 的 10 条，并强调：

1. 零第三方运行时依赖（`src/` 只允许 stdlib）。
2. 所有副作用必须经过 `ToolDispatcher`，先 `tool.intent` 后执行，再 `tool.completed/failed`。
3. 模型只能 `propose_completion`；**只有 `CompletionGate` 能决定完成**。
4. 安全配置分层，项目级只能收紧；不要加绕过。
5. 编辑精确匹配，禁止模糊补丁。
6. 未跑过验证不得声称测试通过。
7. 所有文件入口走 `paths.resolve_in_workspace`，不要自己拼路径判断。
8. 已存在文件必须读后写 + 写前/写后哈希；替换成功后才登记回滚。
9. 测试类 claim 必须引用通过的 L3；L0 失败必须阻断完成。
10. 事件日志是唯一真相：只容忍尾部 torn write；中间损坏/seq 缺口必须报错。
11. **不削弱测试来让 CI 变绿**；不为通过而改探针断言。
12. 新增/修改行为必须同时更新：实现 + 回归测试 + 文档（`docs/REVIEW-RESPONSE.md` 或 `docs/REPORT-AND-HANDBOOK.md`）+ 必要时 `AGENTS.md`。

---

## 8. 接到"根据评审报告修改"时的标准流程

1. **读报告与状态**：读本文件 §2/§3；`git status`、`git log --oneline -5`、`git branch -vv`，确认当前分支不是 `main`。
2. **分类**：把评审项分成 P0/P1（必修、阻塞合并）与 P2/延后（记录、不阻塞）。第 3 轮只有 §3.1/§3.2 两个 P1。
3. **先写失败测试**（TDD）：按评审给出的竞争注入方式（monkeypatch `os.replace`、写坏 tail 的 bytes fixture 等）新增测试；先运行确认**在修复前失败**，把失败输出记录下来。
4. **最小实现**：
   - P1-A：`atomic_write(create=True)` 改为 `O_CREAT|O_EXCL` 独占创建；Update/rollback 明确 best-effort 并加 workspace 锁 + 文档收缩承诺；回滚注入测试。
   - P1-B：`EventLog.open()` 在 append 前修复/截断尾部（无换行 fragment 截断；有效无换行补 `\n`；换行坏 JSON 仍 fail closed）；异常路径释放 lock。
5. **全量验证**：

   ```bash
   python -m pytest -q            # 期望 >= 64 passed（新测试使其增加）
   jinlin-agent eval --suite all  # 18/18
   ```

6. **CI**：推到 `review/**` 分支，等 Windows + Ubuntu 两个 job 都绿；记录 run URL。
7. **文档**：
   - `docs/REVIEW-RESPONSE.md` 增加本轮（第 3 轮）整改表；
   - 更新 `docs/REVIEW-SUBMISSION.md`（或新建 `docs/REVIEW-SUBMISSION-ROUND3.md`）给审查者；
   - 更新 `README.md`/`docs/REPORT-AND-HANDBOOK.md` 中受影响的数字/限制/承诺（尤其"绝不覆盖"这类过强表述必须改准确）。
8. **提交与交付**：commit（英文、语义化）→ push `review/followup-fixes` → 告诉所有者：分支、HEAD、CI run、还有哪些未做；**不要**自行 merge 到 main。
9. **诚实性**：修复不了的项（例如对任意外部进程的原子 CAS）要在文档里收缩承诺并说明剩余风险，不要把 best-effort 写成绝对保证。

---

## 9. 已知陷阱（踩过的坑，直接复用）

1. **Windows 中文编码**：子进程默认 cp936；必须 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8:replace`，父进程显式 `encoding="utf-8", errors="replace"`。
2. **pytest 9 + 受限令牌**：会在仓库根建 `pytest-cache-files-*`，chmod/rename 被拒后污染收集；已通过 `PYTEST_ADDOPTS=-p no:cacheprovider` + `TMP/TEMP` 重定向解决。
3. **同秒 stale `.pyc`**：CPython 用 mtime(秒级)+size 校验；快速"改代码→跑测试"可能跑旧字节码。已通过编辑后清理 `__pycache__/<stem>.*.pyc` 修复；新增编辑路径要复用 `_invalidate_bytecode`。
4. **`.cmd` shim**：Windows 上 `pytest`/`npm` 是 `.cmd`，不要 `shell=True`；`resolve_command` 会拒绝 shim 或解析为真实入口；`python`/`python3` 有 fallback。
5. **沙箱降级**：L1 构造失败自动 L0；报告和 `sandbox.mode` 必须一致；不要假设 L1 一定可用。
6. **Low IL 限制**：workspace 是 Medium 标签，Low IL 写不了；不要为追求"更低完整性"重标用户 workspace。
7. **证据语法**：测试类 claim 必须引用 `ver_*`/`artifact_*`/`L3 tests`/测试命令；只引文件路径会被拒。
8. **事件日志**：读取允许无换行 torn tail，且 `open()` 已在 append 前修复（截断/补换行）；中间坏/空行/seq 缺口必须 fail closed。
9. **回滚边界**：只回滚本事务写后 hash 仍匹配的内容；不一致时 `RECOVERY_UNCERTAIN`，不要静默覆盖。**所有放回（trash/backup → 原路径）必须走 `_restore_no_clobber`（`os.link` create-if-absent）**，绝不 `exists()` + `os.replace`——那是第 4 轮复核抓到的缺陷模式。
10. **`main` 保护**：评审明确要求分支隔离；直接改 main 会被拒。
11. **CI 平台差异**：仅在 Windows 验证过 L1 沙箱；Linux 上 `test_sandbox_win.py` 必须跳过（模块 import 在 Windows-only，tests 里已做 platform guard）。
12. **API/网络**：GitHub HTTP 可能被本地代理拦截；GitHub API 匿名有速率限制；CI 日志下载需凭据。
13. **真实模型**：模型 id 是 `deepseek-flash`；不要把 key 写进配置/日志；用完提醒所有者轮换。
14. **eval 目录污染**：`jinlin-agent eval --workspace .` 会在当前目录生成 fixture；默认（不带 workspace）用临时目录。

---

## 10. 未完成事项（评审已同意延后，不是本次阻塞项）

| 项 | 现状 |
|---|---|
| 多文件**崩溃**事务（transaction manifest + 启动恢复 + kill 注入） | 未实现；并发回滚已做所有权校验 |
| 通用 Claim-Evidence 语义匹配（`claim_type`/`evidence_type`） | 未实现；仅测试类 claim 有 L3 约束 |
| 最终 messages 二次 Token 预算闭环 / required 超预算 `BUDGET_EXCEEDED` | 未实现 |
| Artifact/自由文本统一递归脱敏 | 未实现（仅事件参数顶层键） |
| 事件 hash chain | 延后 |
| 30–50 个独立真实仓库任务评测 + 指标（Verified Completion / False Success / Regression…） | 未做 |
| 覆盖率 / CI 趋势 | 未做（已有 junit 报告 artifact） |
| CI Python 3.11/3.12 矩阵 | 未做（当前 3.13） |

---

## 11. 关键文件索引（排障从这看）

| 想看什么 | 文件 |
|---|---|
| CLI 行为/退出码 | `src/jinlin_agent/cli.py` |
| 完成流程/预算/循环 | `src/jinlin_agent/loop.py` |
| 工具执行顺序/审批/artifact | `src/jinlin_agent/runtime.py` |
| 验证阶梯/完成门 | `src/jinlin_agent/verify.py` |
| 路径边界 | `src/jinlin_agent/paths.py`、`policy.py` |
| 编辑与事务 | `src/jinlin_agent/tools/patch.py` |
| 事件/恢复 | `src/jinlin_agent/events.py`、`session.py`、`state.py` |
| 上下文/提示 | `src/jinlin_agent/context.py`、`prompt.py` |
| 沙箱 | `src/jinlin_agent/sandbox/windows.py`（L1 说明在文件头） |
| 模型 | `src/jinlin_agent/model/openai_compat.py`、`model/mock.py` |
| 探针 | `src/jinlin_agent/evalkit.py`、`tests/test_eval.py` |
| 安全/完成门回归 | `tests/test_security.py` |
| 历史与承诺 | `docs/REVIEW-RESPONSE.md`、`docs/REVIEW-SUBMISSION.md`、`docs/REPORT-AND-HANDBOOK.md`、`AGENTS.md` |

---

## 12. 接手者第一次会话 Checklist

- [ ] 读本文件 §1–§9；
- [ ] `git status` 干净；`git branch -vv` 确认在 `review/followup-fixes`（不是 `main`）；
- [ ] `python -m pip install -e . && python -m pip install pytest`；
- [ ] `python -m pytest -q` = 64 passed；`jinlin-agent eval --suite all` = 18/18；
- [ ] 打开 `JINLIN_AGENT_SUBMISSION_REVIEW.md`，确认两个 P1 与 §3 一致；
- [ ] 按 §3.1 先写 3 个竞争注入测试（先看失败），再实现；
- [ ] 按 §3.2 先写 3 个 open/append/reopen + lock 测试（先看失败），再实现；
- [ ] 全量 pytest + eval 通过；推分支；等 CI 双平台绿；
- [ ] 更新 `docs/REVIEW-RESPONSE.md` / 新提交报告；告诉所有者 HEAD 与 CI URL，**不要自己合 main**。

---

## 13. 交接自测（能答上来才算真的了解项目）

1. 为什么模型不能直接把会话置为 `completed`？它只能调用什么？谁来决定？
2. 事件日志在什么情况下允许"忽略损坏"，什么情况下必须 `SESSION_CORRUPTED`？
3. `atomic_write(expected_sha256=...)` 的 TOCTOU 窗口为什么无法对任意外部进程完全消除？评审允许的替代方案是什么？
4. 为什么 `EventLog.open()` 必须在 append 前修复 torn tail？修复的三种尾部情况分别怎么处理？
5. `create=True` 当前为什么不是原子 no-clobber？`O_CREAT|O_EXCL` 如何修复？
6. 路径边界为什么要检查**不存在的叶子**的父目录？Windows 上要处理什么？
7. 为什么 `.pyc` 会导致"改了代码但测试跑旧代码"？修复点在哪？
8. 完成门在"测试类 claim"上比普通 claim 多要求什么？
9. 项目为什么把沙箱等级写进事件和报告？L1 不可用时行为是什么？
10. 当前 `main` 和 `review/followup-fixes` 的 SHA 是多少？第 3 轮评审的两个 P1 是什么？

---

## 14. 快速命令速查

```bash
# 状态
git status -sb && git log --oneline -5 && git branch -vv

# 测试
python -m pytest -q
python -m pytest tests/test_security.py -q
jinlin-agent eval --suite all

# 自检
jinlin-agent doctor
jinlin-agent trace <sid> --tail 80
jinlin-agent stats <sid>

# 推分支（不改 main）
git add -A && git commit -m "fix(review): ..." && git push origin review/followup-fixes

# 合并（仅所有者/评审确认后）
git checkout main && git merge --ff-only review/followup-fixes && git push origin main
```

---

## 15. 给所有者的交接说明

- 把本文件（`AGENT.md`）与最新评审报告（`JINLIN_AGENT_SUBMISSION_REVIEW.md`）一起交给接手 AI；
- 提醒它：**先读本文件，再动手；不要直接改 main；两个 P1 修完要跑 pytest/eval/CI 并更新评审响应**；
- 接手 AI 若在本机同一仓库工作，直接说"按 AGENT.md 和评审报告执行"即可；若在另一环境，需要给它仓库访问 + 本文件 + 评审报告 + DeepSeek key（通过环境变量提供，不要写进文件）。
