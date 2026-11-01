# 整改提交报告（供独立审查）

| 项 | 值 |
|---|---|
| 仓库 | `hejinlin0824/jinlin-agent` |
| 提交分支 | `review/followup-fixes`（**未改动 `main`**） |
| 基线（`main`） | `85854f1` |
| 代码 HEAD | `9a935d4`（本报告提交后 HEAD 再 +1，代码内容不变） |
| 对应复核 | `JINLIN_AGENT_REVIEW_FOLLOWUP.md`（2026-11-01） |
| 提交日期 | 2026-11-01 |
| 提交范围 | 复核要求的 3 个必修复项 + 文档结论修正 + CI 工作流 |
| 合并建议 | 审查确认后 fast-forward 到 `main`（§8） |

---

## 1. 结论摘要

| 复核要求 | 状态 | 证据 |
|---|---|---|
| §3.1 多文件补丁回滚不得删除/覆盖并发写入 | **已修复** | 事务化写入 + 写后哈希所有权校验；4 条并发回归测试 |
| §3.2 L5 `exit_code=None` 必须失败关闭 | **已修复** | 未知退出状态/超时/未知非零/exit0 含失败 全部判 failed；回归测试 |
| §3.3 事件日志只容忍**无换行结尾**的 torn tail | **已修复** | bytes 读取 + 换行判定；坏尾/空行/schema/session/seq 全部 `SESSION_CORRUPTED`；6 条回归测试 |
| §3.4 收紧 Claim-Evidence 的结论表述 | **已修正** | 明确"仅测试类 Claim 有 L3 证据约束；通用语义匹配未实现" |
| §4 CI/可复现记录缺失 | **已补** | `.github/workflows/ci.yml`，Windows + Ubuntu 双平台，junit + eval artifact |
| §5 由独立评审者确认后再合并 | **遵守** | 只推 `review/followup-fixes`，`main` 保持 `85854f1` |

额外收益：CI 在 Ubuntu 上暴露了一个本地未复现的真实缺陷（同一秒内编辑 `.py` 导致旧 `.pyc` 继续生效，L3 跑旧代码），已修复并加回归测试（§5）。

当前验证：**64 passed**（42 基础 + 22 安全/完成门/事务回归）、**18/18 probes passed**、CI 双平台绿。

---

## 2. 审查者如何复核（5 分钟）

```bash
git fetch origin
git checkout review/followup-fixes
git log --oneline 85854f1..HEAD      # 7 个提交
diff 见 §6（492 insertions, 66 deletions）

python -m pip install -e .
python -m pip install pytest
python -m pytest -q                  # 期望：64 passed
jinlin-agent eval --suite all        # 期望：18/18 probes passed
```

CI 记录（代码提交 `9a935d4`，双平台 success）：
<https://github.com/hejinlin0824/jinlin-agent/actions/runs/41270143374>

建议按以下顺序看代码（每处都有对应测试）：

| 优先级 | 文件 / 函数 | 看什么 |
|---|---|---|
| 1 | `src/jinlin_agent/tools/patch.py::apply_patch` 写事务 + `_rollback_transaction` | 回滚只接受"本事务写入且哈希仍匹配"的变更 |
| 2 | `src/jinlin_agent/verify.py::Verifier._level5` | `exit_code is None` / timeout / 未知非零的 fail-closed 分支 |
| 3 | `src/jinlin_agent/events.py::_iter_events_checked` | bytes 读取、`ends_with_newline`、空行/schema/session/seq 校验 |
| 4 | `tests/test_security.py` | 22 条安全/完成门/事务回归 |
| 5 | `.github/workflows/ci.yml` | Windows + Ubuntu 双平台 |

---

## 3. §3.1：多文件补丁回滚的并发安全

### 3.1.1 原问题

旧实现先登记备份、再执行写；写入阶段抛错后无条件恢复所有登记项。导致：

- Add 因"目标已被并发创建"而失败时，回滚看到文件存在就删除**别人的文件**；
- Update/Delete 检测到外部修改后抛错，回滚却用旧内容**覆盖外部修改**；
- 回滚异常被 `except: pass` 静默吞掉。

### 3.1.2 新实现

写入阶段只在**操作成功之后**登记一条事务记录，并记录本事务产生的哈希：

```text
Add     : atomic_write(create=True) 成功后登记 {kind:add, path, after_sha}
Update  : atomic_write(expected=old_sha) 成功后登记 {kind:update, path, before, after_sha}
Delete  : 文件哈希校验 → os.replace(path, backup) → 校验 backup 哈希
          → 成功登记 {kind:delete, path, backup, before_sha}
          失败（移动后哈希不符）→ 若 path 不存在则把 backup 移回，报 FILE_CHANGED_EXTERNALLY
```

回滚（`_rollback_transaction`）逐条反向处理，并且**只回滚仍属于本事务的状态**：

| 类型 | 回滚条件 | 不满足时 |
|---|---|---|
| Add | 文件存在且 `sha == after_sha` | 保留文件，列入 `rollback_incomplete` |
| Update | 文件存在且 `sha == after_sha` | 保留外部内容，列入 `rollback_incomplete` |
| Delete | `path` 不存在且 backup 存在 | 保留现场，列入 `rollback_incomplete` |

- 回滚不再 `except: pass`；不完整时返回 `AgentError(code="RECOVERY_UNCERTAIN", details={"rolled_back": [...], "rollback_incomplete": [...]})`。
- 事务成功后删除 Delete 的备份文件；事务失败则按上表处理。

### 3.1.3 验收对照

| 复核要求 | 实现 | 测试 |
|---|---|---|
| Add 并发冲突不得删除对方文件 | 冲突在 `create=True` 抛错，此时**未登记** Add 记录 | `test_apply_patch_add_conflict_does_not_delete_external_file` |
| Update 冲突不得覆盖外部修改 | 冲突在 `expected_sha256` 抛错，未登记；已登记项回滚前复核哈希 | `test_apply_patch_update_conflict_does_not_restore_over_external_edit` |
| 只回滚本事务自己的修改，并报告不完整 | `_rollback_transaction` 哈希所有权检查 + `RECOVERY_UNCERTAIN` | `test_apply_patch_rollback_only_reverts_own_mutations` |
| Delete 冲突保留外部文件 | 重命名→校验→失败恢复；不裸 `unlink` | `test_apply_patch_delete_conflict_preserves_external_file` |

> 明确边界：这解决的是**并发回滚误删/误覆盖**；跨文件**崩溃**原子性（进程被强杀/断电后的 transaction manifest + 启动恢复）仍未实现，见 §7。

---

## 4. §3.2：L5 未知退出状态 fail-closed

### 4.1 判定表（新的 `_level5`）

| 当前状态 | 判定 |
|---|---|
| `timed_out=True` | failed（finding: timed out） |
| `exit_code is None` | failed（finding: returned no exit code，附 `RunResult.error`） |
| 新增失败/错误 nodeid | failed（列出 nodeid） |
| `exit_code == 0` 但输出仍含 FAILED/ERROR nodeid | failed（不信任矛盾输出） |
| `exit_code != 0` 且失败集合 ⊆ 基线已知失败集合 | passed（已知失败继续存在） |
| `exit_code != 0` 且无法证明是已知失败 | failed（unknown failure set） |

测试：`test_l5_none_exit_code_fails_closed`（新增）、`test_l5_detects_changed_failure_identity`、`test_parse_test_output_extracts_failure_ids`。

---

## 5. §3.3：事件日志尾部判定

### 5.1 新规则

- 以 **bytes** 读取，保留文件是否以 `\n` 结尾；
- 只有**最后一条且文件末尾没有换行**的解析失败片段，才视为 torn write；
- 以下情况全部抛 `SESSION_CORRUPTED`：
  - 换行终止的坏 JSON；
  - 事件之间出现空行；
  - `schema_version != 1`；
  - `payload` 不是对象 / 缺 `type`/`seq`；
  - 同一日志内 `session_id` 不一致；
  - `seq` 不连续（缺口/重复）。

### 5.2 测试（原 torn-tail 测试已修正为真实"无换行"样本）

| 测试 | 构造 |
|---|---|
| `test_torn_tail_without_newline_is_tolerated` | `event\n` + `{"seq":`（末尾无换行） |
| `test_newline_terminated_corrupt_tail_is_rejected` | `event\n` + `{broken}\n` |
| `test_blank_line_between_events_is_rejected` | `event\n\nevent\n` |
| `test_schema_and_session_mismatch_is_rejected` | 非法 schema / 混合 session_id |
| `test_mid_log_corruption_is_reported` / `test_seq_gap_is_reported` | 中间损坏 / seq 缺口 |

---

## 6. 额外发现：CI 暴露的"同秒 stale `.pyc`"缺陷（已修复）

**现象**：Ubuntu CI 上 P2/E2 探针失败：文件已修复，但 L3 仍报 `test_add` 失败。

**根因**：CPython 的 `.pyc` 以"源文件 mtime（秒级）+ size"校验。baseline 导入后，若编辑发生在**同一秒**且新旧文件长度相同（`a - b` → `a + b`），旧 `.pyc` 仍被视为有效，测试跑的是旧代码。Windows 上因时序差异未复现。

**修复**：`atomic_write` 成功后按源文件清理 `__pycache__/<stem>.*.pyc`（`_invalidate_bytecode`），并新增 `test_edit_invalidates_stale_bytecode`。

**意义**：这不是测试环境问题，而是 harness 的真实正确性缺陷——任何"快速连续修改并验证"的场景都可能受影响。

---

## 7. 变更范围与验证

### 7.1 Diffstat（`85854f1..9a935d4`）

```
 .github/workflows/ci.yml        |  42 +++++++++
 .gitignore                      |   3 +
 AGENT.md                        |   2 +-
 AGENTS.md                       |   2 +-
 README.md                       |   2 +
 docs/REPORT-AND-HANDBOOK.md     |   2 +-
 docs/REVIEW-RESPONSE.md         |  38 ++++++---
 src/jinlin_agent/evalkit.py     |  38 ++++++++-
 src/jinlin_agent/events.py      |  73 +++++++++++-----
 src/jinlin_agent/tools/patch.py | 144 ++++++++++++++++++++++++++-----
 src/jinlin_agent/tools/shell.py |   3 +
 src/jinlin_agent/verify.py      |  14 ++-
 tests/test_sandbox_win.py       |  11 ++-
 tests/test_security.py          | 184 +++++++++++++++++++++++++++++++++++++++-
 14 files changed, 492 insertions(+), 66 deletions(-)
```

提交列表（7 个）：

```
cf3cc2a fix(review): make patch rollback concurrent-safe, fail-closed L5 unknown exit, strict event tail validation; add CI workflow
4b4409a ci: skip Windows-only sandbox module import during collection on non-Windows runners
d55622a ci: platform-neutral test commands in probes; python/python3 fallback resolution
75fd930 test: richer failure diagnostics for scripted probes (CI triage)
6faca6b test: include verification summary in probe failure digest
e90be78 fix: invalidate cached bytecode after editing a .py file (same-second stale pyc on fast CI)
9a935d4 docs: CI badge, follow-up review status, bytecode-invalidation note
```

### 7.2 验证记录（本机）

```
环境：Windows 11，Python 3.13.15，无 Docker/WSL
python -m pytest -q          -> 64 passed
jinlin-agent eval --suite all -> 18/18 probes passed
```

### 7.3 CI 记录

- Workflow：`.github/workflows/ci.yml`（push 到 `main`/`review/**`/`dev` 与 PR 触发）
- 矩阵：`windows-latest`、`ubuntu-latest`，Python 3.13
- 内容：pytest（junit.xml）+ 离线 `eval --suite all` + 报告 artifact
- 结果：代码提交 `9a935d4` 双平台 **success**
  <https://github.com/hejinlin0824/jinlin-agent/actions/runs/41270143374>

---

## 8. 未完成 / 非声明（请计入审查）

| 项 | 状态 |
|---|---|
| 多文件崩溃事务（transaction manifest + 启动恢复 + kill 注入） | **未实现**；并发回滚已安全，崩溃原子性仍不保证 |
| 通用 Claim-Evidence 语义匹配 | **未实现**；仅测试类 Claim 有 L3 证据约束 |
| 最终 messages 的二次 Token 预算闭环 | 未实现；required 块仍可能挤爆预算 |
| Artifact / 自由文本递归脱敏 | 未实现；仅事件参数顶层键脱敏 |
| 事件 hash chain | 延后 |
| 30–50 个独立真实任务评测 | 未做 |
| 覆盖率 / 趋势数据 | 未做 |

本次提交**不声称**：达到生产级隔离、通过通用基准、完成上述任何"未实现"项。

---

## 9. 合并建议

```bash
git checkout main                 # 当前 85854f1
git merge --ff-only review/followup-fixes
git push origin main
```

若要保留审查痕迹，也可走 PR：
<https://github.com/hejinlin0824/jinlin-agent/pull/new/review/followup-fixes>

---

## 10. 审查清单（可直接勾选）

- [ ] `git checkout review/followup-fixes` 后 `python -m pytest -q` = 64 passed
- [ ] `jinlin-agent eval --suite all` = 18/18
- [ ] 阅读 `patch.py` 写事务与 `_rollback_transaction`，确认回滚只回滚本事务且保留外部内容
- [ ] 阅读 `verify.py::_level5`，确认 `exit_code=None` / timeout / 未知非零均 failed
- [ ] 阅读 `events.py::_iter_events_checked`，确认只容忍无换行的尾部片段
- [ ] 检查 `tests/test_security.py` 是否覆盖复核提出的 4 个并发场景
- [ ] 检查 CI 双平台结果（§7.3）
- [ ] 确认 `main` 未被改动（仍为 `85854f1`）
- [ ] 确认 §8 的未完成项没有被文档夸大

---

## 11. 相关材料

- 首轮评审：`JINLIN_AGENT_REVIEW.md`
- 复核报告：`JINLIN_AGENT_REVIEW_FOLLOWUP.md`
- 整改记录：`docs/REVIEW-RESPONSE.md`（v1.2）
- 技术报告 + 产品手册：`docs/REPORT-AND-HANDBOOK.md`（v1.1）
- Agent 自述 / handoff 契约：`AGENT.md`
