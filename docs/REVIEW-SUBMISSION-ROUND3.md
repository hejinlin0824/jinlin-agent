# 第 3 轮复核整改提交报告（供独立审查）

| 项 | 值 |
|---|---|
| 仓库 | `hejinlin0824/jinlin-agent` |
| 提交分支 | `review/followup-fixes`（**未改动 `main`**，仍为 `85854f1`） |
| 基线（`main`） | `85854f1` |
| 代码 HEAD | `22d4c7d`（本报告与其余文档提交后 HEAD 再 +N，代码内容不变） |
| 对应复核 | `JINLIN_AGENT_SUBMISSION_REVIEW.md`（2026-11-01，第 3 轮：Request changes） |
| 提交日期 | 2026-11-01 |
| 提交范围 | 复核 §3 的两个 P1 全部处理 + 复核 §5 建议的竞争注入/恢复集成测试 + 文档承诺收缩 |
| 合并建议 | 审查确认后 fast-forward 到 `main`（§8） |

---

## 1. 结论摘要

| 第 3 轮复核要求 | 状态 | 证据 |
|---|---|---|
| §3.1 `create=True` 原子 no-clobber | **已修复** | 新建路径改用 `os.open(O_CREAT\|O_EXCL)`，完全不经过 `os.replace`；竞争失败 → `PATCH_CONFLICT`；2 条竞争注入测试 |
| §3.1 Update `expected_sha256` TOCTOU | **已收缩承诺 + 锁保护**（复核明确允许的方案） | 写前复核只声称"缩小"；新增 workspace 写锁串行化 Jinlin 会话（`WORKSPACE_LOCKED` 可重试）；残余风险写入安全模型 §16；2 条测试 |
| §3.1/§3.3 回滚 check-then-act | **已修复（先移动后校验）** | 回滚不再原地 unlink/覆盖：先原子改名到事务级 trash → 校验归属 → 才删除/替换；不匹配原样移回 + `RECOVERY_UNCERTAIN`；Delete 恢复用 `os.link`（create-if-absent）；1 条竞争注入测试 |
| §4 `EventLog.open()` 追加前修复 torn tail | **已修复** | 无换行坏碎片 → 截断到最后完整事件（fsync）；完整 JSON 缺换行 → 补 `\n`（fsync）；换行坏 JSON → 仍 fail-closed；`open()` 失败释放锁；3 条集成回归测试 |
| §5.5 损坏日志打开失败释放锁 | **已修复** | `test_open_failure_releases_session_lock` |
| §2 L5 / CI / 路径 / 回滚所有权（已通过项） | **保留，未回退** | 全部原有测试继续通过（64 → 72 条） |

当前验证：**72 passed**（42 基础 + 30 安全/完成门/事务回归）、**18/18 probes passed**、CI 双平台结果见 §6。

诚实性声明（按复核 §3.2 的工程边界）：对**不受本程序控制的外部进程**，跨平台普通文件 API 无法提供"按内容哈希的原子 compare-and-swap"。Update 路径的写前哈希复核把"检查→替换"窗口缩到微秒级，但**不等于消除**；消除的是 Jinlin 会话之间的竞争（workspace 写锁，硬保证）。该残余风险已如实写入 `docs/REPORT-AND-HANDBOOK.md` §16 第 11 条，不再声称"绝不覆盖"。新建文件与回滚路径分别是文件系统级原子 no-clobber / 内容保全（先移动后校验），无此前提限制。

---

## 2. 审查者如何复核（5 分钟）

```bash
git fetch origin
git checkout review/followup-fixes
git log --oneline dbeb6af..22d4c7d   # 1 个代码提交

python -m pip install -e .
python -m pip install pytest
python -m pytest -q                  # 期望：72 passed
jinlin-agent eval --suite all        # 期望：18/18 probes passed
```

CI 记录：见 §6（推送后由所有者/审查者核对双平台结果）。

建议按以下顺序看代码（每处都有对应测试）：

| 优先级 | 文件 / 函数 | 看什么 |
|---|---|---|
| 1 | `src/jinlin_agent/tools/patch.py::_atomic_create_new` | `O_CREAT\|O_EXCL` 独占创建；`FileExistsError` → `PATCH_CONFLICT`；失败清理半写文件 |
| 2 | `src/jinlin_agent/tools/patch.py::_rollback_transaction` | 先移动后校验（trash 改名 → 哈希归属 → 才删/换）；`os.link` 恢复 Delete |
| 3 | `src/jinlin_agent/tools/patch.py::_workspace_write_guard` + 三个 handler 包装 | 写阶段持 `<workspace>/.jinlin/write.lock`；竞争 → `WORKSPACE_LOCKED` |
| 4 | `src/jinlin_agent/events.py::EventLog.open/_repair_tail_before_append` + `_parse_log_data` | 读取与修复共用同一解析器；三种尾部处置；失败释放锁 |
| 5 | `tests/test_security.py`（新增 8 条） | §3/§4 的测试对照表 |

---

## 3. §3.1：create-only 原子性与 TOCTOU 边界

### 3.1.1 Add：从"检查后替换"到"独占创建"

旧实现的顺序是 `if create and path.exists(): raise` → `os.replace(tmp, path)`，两步之间存在真实竞争窗口。新实现：

```text
_atomic_create_new(path, data):
    os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o666)   # 原子建立+判定，双平台
    FileExistsError -> AgentError(PATCH_CONFLICT)
    写入 + fsync + 写后校验 + 失效 __pycache__（失败则删除自己创建的半写文件）
```

- Add 路径**不再调用 `os.replace`**，`test_apply_patch_add_no_clobber_at_replace_window` 直接断言这一点（旧实现下该测试失败：os.replace 被调用且覆盖了注入的外部文件）；
- 在独占创建前瞬间注入并发创建者（monkeypatch `os.open`），断言 `PATCH_CONFLICT` 且外部内容完好：`test_apply_patch_add_conflict_when_creator_wins_exclusive_create`。

### 3.1.2 Update：收缩承诺 + workspace 写锁

按复核允许的方案：

- `atomic_write` docstring 与文档改为准确表述：写前哈希复核**缩小** TOCTOU 窗口，不等于消除；
- 写阶段（`apply_patch` / `edit_file` / `write_file` 全过程）持 `<workspace>/.jinlin/write.lock` 顾问锁：**多个 Jinlin 会话**在同一工作区上串行，后来者得到可重试的 `WORKSPACE_LOCKED`（新稳定错误码）——对会话间竞争这是硬保证；
- 任意外部进程不受锁约束，"最后窗口内修改被覆盖且无法事后检测"写入安全模型（`REPORT-AND-HANDBOOK.md` §16 第 11 条）；
- 写后校验与 `RECOVERY_UNCERTAIN` 语义保留。

测试：`test_editing_tools_serialize_on_workspace_write_lock`（持锁 → 第二会话被拒且文件未动 → 释放后同一补丁成功）、`test_apply_patch_update_toctou_after_hash_check`（在最后窗口注入外部写入，钉住已收缩契约的实际行为，防止未来实现悄悄变更语义却不改文档）。

### 3.1.3 回滚：先移动后校验

回滚不再对活动路径做"检查后原地 unlink/覆盖"：

| 类型 | 新回滚动作 | 外部内容出现时 |
|---|---|---|
| Add | `os.replace(path → trash)` → 校验 trash 哈希 = 本事务写后哈希 → `trash.unlink()` | 哈希不符 → `os.replace(trash → path)` 原样移回，`rollback_incomplete` |
| Update | 同上 trash 步骤 → `atomic_write(path, before, create=True)`（O_EXCL）→ 删 trash | 恢复写失败且路径已被外部占用 → 保留双方内容（trash 留存），`rollback_incomplete` |
| Delete | `os.link(backup, path)`（双平台 create-if-absent）→ 删 backup | `FileExistsError` → 并发重建者胜出，`rollback_incomplete`；不支持硬链的文件系统回退为"再查存在 + rename"（best-effort，已注释） |

竞争注入测试 `test_rollback_after_check_preserves_external_content`：在 trash 改名前把目标改为外部内容 → 外部内容随 trash 一起被移动 → 哈希不匹配 → 原样移回，断言外部内容完好 + `RECOVERY_UNCERTAIN` + `rollback_incomplete` 含该路径。（旧实现下该测试失败：restore 直接覆盖。）

---

## 4. §4：`EventLog.open()` 追加前修复尾部

### 4.1 修复规则（读取与修复共用 `_parse_log_data`，规则绝不分叉）

| 末尾状态 | `open()` 行为 |
|---|---|
| 以换行结束（或空文件） | 无需修复 |
| 无换行、无法解析的碎片（真实 torn write） | 截断到最后一条完整事件末尾，`flush + fsync` |
| 无换行、完整有效的最后一条 JSON | 补写 `\n`，`flush + fsync` |
| 换行终止的坏 JSON / 空行 / schema / session / seq 错误 | **fail-closed**：`SESSION_CORRUPTED`，不自动修复 |

`open()` 校验失败时释放已取得的会话锁（`try/except` 中 `self._lock.release()`），不遗留锁句柄。

### 4.2 测试（复核 §4.2 要求的两条 + 锁释放）

| 测试 | 构造 | 断言 |
|---|---|---|
| `test_open_repairs_torn_tail_before_append` | `event\n` + `{"seq":` → open → append → close | `read_events()` = `[1, 2]`；文件以 `\n` 结尾；每行均可解析（torn 碎片已消失） |
| `test_open_repairs_missing_trailing_newline` | 一条有效 JSON 无 `\n` → open → append → close | 两条独立事件，而不是拼接行 |
| `test_open_failure_releases_session_lock` | `event\n` + `{broken}\n` | `open()` 抛 `SESSION_CORRUPTED` 后，第二个 `EventLog` 实例能立即取得锁 |

---

## 5. 变更范围

### 5.1 Diffstat（`42dfe94..22d4c7d`，代码提交）

```
 src/jinlin_agent/errors.py      |   1 +      (WORKSPACE_LOCKED)
 src/jinlin_agent/events.py      | 168 +++++-----  (torn-tail 修复 + 共用解析器)
 src/jinlin_agent/locks.py       |  71 ++++++++  (SessionLock 提为独立模块，供 events 与 tools 共用)
 src/jinlin_agent/tools/patch.py | 187 +++++++--- (O_EXCL / 写锁 / 先移动后校验回滚)
 tests/test_security.py          | 198 ++++++++++ (8 条新回归)
 5 files changed, 523 insertions(+), 102 deletions(-)
```

另有文档提交（本报告、`REVIEW-RESPONSE.md` v1.3、`REPORT-AND-HANDBOOK.md` §7.1/§9/§16、`README.md` §4、`AGENT.md` 状态更新），不触碰 `src/` 与 `tests/`。

### 5.2 测试增量（64 → 72）

| 新测试 | 覆盖 |
|---|---|
| `test_open_repairs_torn_tail_before_append` | P1-B 恢复路径 1 |
| `test_open_repairs_missing_trailing_newline` | P1-B 恢复路径 2 |
| `test_open_failure_releases_session_lock` | §5.5 锁释放 |
| `test_apply_patch_add_no_clobber_at_replace_window` | Add 不再经过 os.replace |
| `test_apply_patch_add_conflict_when_creator_wins_exclusive_create` | O_EXCL 竞争失败语义 |
| `test_apply_patch_update_toctou_after_hash_check` | Update 已收缩契约的实际行为 |
| `test_editing_tools_serialize_on_workspace_write_lock` | workspace 写锁串行化 |
| `test_rollback_after_check_preserves_external_content` | 回滚竞争注入 |

全部按"先写测试、确认在旧实现上失败、再实现转绿"完成（TDD 记录：5 个 P1-A 测试与 3 个 P1-B 测试在修复前均失败，失败形态与复核预测一致）。

### 5.3 验证记录（本机）

```
环境：Windows 11，Python 3.13.15
python -m pytest -q          -> 72 passed
jinlin-agent eval --suite all -> 18/18 probes passed
```

---

## 6. CI

- Workflow：`.github/workflows/ci.yml`（push 到 `main`/`review/**`/`dev` 与 PR 触发），矩阵 `windows-latest` / `ubuntu-latest`，Python 3.13；
- 本轮代码提交 `22d4c7d` 的 run：<（推送后填写/见分支 Actions 页）；
- 判定标准：两个 job 均 success 才视为通过；如失败，修复后再推并更新本节。

---

## 7. 未完成 / 非声明（与上一轮一致，请计入审查）

| 项 | 状态 |
|---|---|
| 多文件**崩溃**事务（transaction manifest + 启动恢复 + kill 注入） | 未实现；本轮进一步收窄了并发回滚窗口，但崩溃原子性仍不保证 |
| 对任意外部进程的原子 compare-and-swap | **不实现**（跨平台普通文件 API 不可能）；已收缩承诺 + workspace 锁 + §16 威胁模型条目 |
| 通用 Claim-Evidence 语义匹配 / 最终 token 预算闭环 / 递归脱敏 / 事件 hash chain | 未实现 |
| 30–50 个独立真实任务评测 / 覆盖率趋势 | 未做 |
| CI Python 3.11/3.12 矩阵 | 未做（复核标注非阻塞） |

---

## 8. 合并建议

两个 P1 已按复核方向处理完毕、全量测试与离线 eval 通过、CI 双平台绿之后：

```bash
git checkout main                 # 当前 85854f1
git merge --ff-only review/followup-fixes
git push origin main
```

---

## 9. 审查清单（可直接勾选）

- [ ] `git checkout review/followup-fixes` 后 `python -m pytest -q` = 72 passed
- [ ] `jinlin-agent eval --suite all` = 18/18
- [ ] `_atomic_create_new`：Add 路径不再调用 `os.replace`（对照 `test_apply_patch_add_no_clobber_at_replace_window`）
- [ ] `_rollback_transaction`：先移动后校验，外部内容不被删除/覆盖（对照注入测试）
- [ ] `_workspace_write_guard`：写阶段持锁，会话间串行；文档承诺已收缩且与实现一致
- [ ] `EventLog.open()`：三种尾部处置正确；换行坏 JSON 仍 fail-closed；失败释放锁
- [ ] 安全模型 §16 第 11 条如实描述残余窗口（未夸大）
- [ ] CI 双平台 success；`main` 未被改动（仍为 `85854f1`）

---

## 10. 相关材料

- 第 3 轮复核：`JINLIN_AGENT_SUBMISSION_REVIEW.md`
- 整改记录：`docs/REVIEW-RESPONSE.md`（v1.3，§0a）
- 安全模型 / 威胁模型：`docs/REPORT-AND-HANDBOOK.md` §7.1、§9、§16
- 前两轮提交报告：`docs/REVIEW-SUBMISSION.md`
- Agent 自述 / handoff 契约：`AGENT.md`
