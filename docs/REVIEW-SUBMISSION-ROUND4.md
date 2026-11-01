# 第 4 轮复核整改提交报告（供独立审查）

| 项 | 值 |
|---|---|
| 仓库 | `hejinlin0824/jinlin-agent` |
| 提交分支 | `review/followup-fixes`（**未改动 `main`**，仍为 `85854f1`） |
| 基线（`main`） | `85854f1` |
| 代码 HEAD | `f1b0cb8`（本报告与其余文档提交后 HEAD 再 +N，代码内容不变） |
| 对应复核 | 《第 3 轮整改复核意见》（2026-11-01：Request changes，P1-1 / P1-2） |
| 提交日期 | 2026-11-01 |
| 提交范围 | 按复核 §6 要求**只处理 P1-1 / P1-2** + 复核明确建议顺手补上的 P2-1（严格 UTF-8）；P2-2 / P2-3 未动，单独排期 |
| 合并建议 | 最后一轮复核确认后 fast-forward 到 `main` |

---

## 1. 结论摘要

| 复核要求 | 状态 | 证据 |
|---|---|---|
| §3 P1-1 Add/Update 回滚恢复覆盖并发新建路径 | **已修复** | 统一 `_restore_no_clobber` helper：`os.link` 原子 create-if-absent 放回 + 删除冗余 source 名；路径被并发占用时**双方保留**（外部在路径、本事务内容在 trash）并返回 `RECOVERY_UNCERTAIN`；参数化注入测试（add/update 两种） |
| §3 P1-2 Delete 移动后 Hash 不匹配恢复的同类窗口 | **已修复** | 同一 helper；恢复成功 → `FILE_CHANGED_EXTERNALLY`（original restored）；路径被占用 → `RECOVERY_UNCERTAIN` + `details.kept_aside` 指明备份名；注入测试覆盖 |
| §6.1 统一 no-clobber restore helper，三个恢复入口全部使用 | **完成** | Add 回滚 mismatch、Update 回滚 mismatch / 恢复写失败、Delete 回滚恢复、Delete move-hash-mismatch、（既有的）Delete 回滚 `os.link` 恢复，全部收敛到 helper |
| §6.2 "trash 已移动、原路径被外部重建"时序的竞争注入测试 | **完成** | `test_rollback_restore_is_no_clobber_when_path_recreated[add]` / `[update]`、`test_delete_move_hash_mismatch_restore_is_no_clobber` |
| §6.3 冲突时保留双方数据，返回 `RECOVERY_UNCERTAIN`，不覆盖任何一方 | **完成** | 测试断言：路径内容 = 外部重建内容；trash/backup 内容 = 移动时刻路径上的实际内容（测试注入的外部改写）且文件留存；错误码 = `RECOVERY_UNCERTAIN` |
| §4 P2-1 严格 UTF-8 解码（复核建议顺手补上） | **已补** | `raw.decode("utf-8")` 严格解码；JSON 字符串值内非法字节 → `SESSION_CORRUPTED`；读取与 `EventLog.open()` 双路径测试 |
| §4 P2-2 / P2-3 | **未动**（按复核 §6 "单独排期"） | 见 §5 |

当前验证：**76 passed**（42 基础 + 34 安全/完成门/事务回归）、**18/18 probes passed**、CI 双平台结果见 §6。

---

## 2. 修复设计

### 2.1 统一 helper

```python
def _restore_no_clobber(source: Path, dest: Path) -> bool:
    os.link(source, dest)      # 原子 create-if-absent：dest 已存在 → 不动它
    os.unlink(source)          # 再删除冗余的 source 名
    # dest 被占用 / 文件系统不支持硬链接 → 返回 False，source 原样保留
```

- 放回动作本身是 `os.link` 的 create-if-absent 语义，**不存在"目标已存在却覆盖"的指令序列**；
- 按复核 §3 的要求，**不用覆盖式 rename 作为静默回退**：硬链接不可用时返回 False，由调用方报告 `RECOVERY_UNCERTAIN`（上一版 Delete 回滚中"重查存在 + rename"的 fallback 已删除）；
- `link` 成功后 `unlink` 失败仅残留一个同名冗余目录项，内容已在目标路径，无正确性影响。

### 2.2 五个恢复入口全部收敛

| 恢复入口（patch.py） | 旧行为 | 新行为 |
|---|---|---|
| Add 回滚，trash 所有权不匹配 | `os.replace(trash, path)`（可覆盖并发重建） | `_restore_no_clobber`；False → 双方保留 + `rollback_incomplete` |
| Update 回滚，trash 所有权不匹配 | 同上 | 同上 |
| Update 回滚，恢复写失败后放回 | `exists()` + `os.replace(trash, path)` | `_restore_no_clobber`；False → 双方保留 |
| Delete 回滚，从 backup 恢复 | `os.link` + **不支持硬链时 `exists()` + `os.replace` fallback** | 统一 `_restore_no_clobber`；False → 保留 backup + `rollback_incomplete`（fallback 已删） |
| Delete 写入阶段，move 后 hash 不匹配（P1-2） | `exists()` + `os.replace(backup, path)` | `_restore_no_clobber`：成功 → `FILE_CHANGED_EXTERNALLY`（original restored）；False → `RECOVERY_UNCERTAIN` + `details={"kept_aside": 备份名}` |

### 2.3 回归测试（全部先在旧实现上确认失败）

| 测试 | 注入时序 | 断言 |
|---|---|---|
| `test_rollback_restore_is_no_clobber_when_path_recreated[add]` | 本事务 Add 后注入外部改写 → 目标随 trash 原子移动（所有权失配）→ trash 移动完成后、放回前，外部**重建**原路径 | 路径 = 外部重建内容（`external-recreated`）；trash 留存 = 移动时刻路径上的实际内容（即注入的 `external-in-window`），未被销毁、可恢复；`RECOVERY_UNCERTAIN` + `rollback_incomplete` |
| `test_rollback_restore_is_no_clobber_when_path_recreated[update]` | 同上（Update 事务） | 同上 |
| `test_delete_move_hash_mismatch_restore_is_no_clobber` | Delete 移动到 backup 前注入外部改写（随 backup 移动、hash 失配）→ 放回前外部重建原路径 | 路径 = 外部重建内容（`external-recreated`）；backup 留存 = 移动时刻路径上的实际内容（注入的 `external-in-backup`），未被销毁；`RECOVERY_UNCERTAIN` |
| `test_invalid_utf8_inside_event_is_rejected` | JSON 字符串值内注入非法 UTF-8 字节（`\xff\xfe`） | `read_events` 与 `EventLog.open()` 均 `SESSION_CORRUPTED` |

**内容描述更正（第 5 轮复核 §2.4 指出）**：trash / backup 中保留的**不是**本事务写入的原始字节——测试桩在移动前注入的外部改写会随目录项一起进入 trash/backup（这正是所有权哈希失配、进入恢复分支的原因）。测试断言与上表已按实际行为表述为"移动时刻路径上的实际内容（注入的外部改写）"。no-clobber 行为的验证不受此影响：断言的是"外部重建的路径不被放回动作替换 + 放回失败后源文件（trash/backup）内容原样留存"。**测试内的断言消息措辞已同步对齐**（消息字符串仅为提示文本，断言逻辑未变）。

复核建议的后续强化（非阻塞，未在本轮实施）：增加一组"trash/backup 保留本事务原始字节、移动完成后再重建目标路径"的注入变体，进一步区分"外部内容随移动进入 trash"与"本事务原始内容滞留 trash"两种现场。

注：修复前运行上述测试均失败，失败形态与复核预测一致。

---

## 3. 变更范围

### 3.1 Diffstat（`4e69a8c..f1b0cb8`，代码提交）

```
 src/jinlin_agent/events.py      |   2 +-   (严格 UTF-8 解码)
 src/jinlin_agent/tools/patch.py |  77 ++++-----  (_restore_no_clobber helper + 5 个恢复入口收敛)
 tests/test_security.py          | 119 ++++++++++  (4 条新回归)
 3 files changed, 168 insertions(+), 30 deletions(-)
```

另有文档提交（本报告、`REVIEW-RESPONSE.md` §0b、`REPORT-AND-HANDBOOK.md` §9/§16 措辞更新、`AGENT.md` 状态），不触碰 `src/` 与 `tests/`。

### 3.2 验证记录（本机）

```
环境：Windows 11，Python 3.13.15
python -m pytest -q          -> 76 passed
jinlin-agent eval --suite all -> 18/18 probes passed
```

---

## 4. 与上一轮承诺的一致性

- 工作区写锁、`O_CREAT|O_EXCL` 建立路径、EventLog 尾部修复、锁释放等**上一轮已通过项未回退**（全部既有测试继续通过）；
- `RECOVERY_UNCERTAIN` 语义不变：仍是"无法证明本事务所有权时保留现场并上报"，本轮只是把"保留现场"从"覆盖式放回"改为真正的双方保留；
- 残余 TOCTOU 的威胁模型表述（`REPORT-AND-HANDBOOK.md` §16 第 11 条）已同步更新：放回动作现在也是 no-clobber，"外部内容被覆盖"的残留路径不再存在；仍然 best-effort 的部分（外部进程在窗口内写内容、随后被本事务内容以外的方式干扰）继续如实声明。

---

## 5. 未完成 / 单独排期（请计入审查）

| 项 | 状态 |
|---|---|
| P2-2 事件日志校验 `session_id` 与 EventLog 实例一致 | 未动（本轮范围外，已排期） |
| P2-3 `_atomic_create_new` 失败清理按路径 unlink 的身份校验 | 未动（本轮范围外，已排期） |
| 多文件崩溃事务 / 通用 Claim-Evidence / token 预算闭环 / 递归脱敏 / hash chain / 30–50 任务评测 / 覆盖率 / CI 3.11+3.12 | 与上轮一致，未动 |

---

## 6. CI 证据（第 5 轮复核 §3.1 要求的验收材料）

| 项 | 值 |
|---|---|
| Run URL | <https://github.com/hejinlin0824/jinlin-agent/actions/runs/41280497556>（Run ID `41280497556`） |
| Run head SHA | `545e5cb`（评审分支 HEAD）；其相对代码提交 `f1b0cb8` 的差异**只有** `AGENT.md` 与 `docs/*.md`，**未触碰 `src/` 与 `tests/`**（`git diff --stat f1b0cb8..545e5cb`：4 个文件，全部为 Markdown 文档） |
| windows-latest / py3.13 | **success**（全部步骤通过，含 "Unit + probe + security tests" 与 "Harness conformance probes"） |
| ubuntu-latest / py3.13 | **success**（同上） |
| Windows pytest（`reports/junit.xml`） | `tests=76 failures=0 errors=0 skipped=0` |
| Ubuntu pytest（`reports/junit.xml`） | `tests=76 failures=0 errors=0 skipped=16`（16 个 skip 为 Windows 专属 L1 沙箱测试模块的平台守卫，与本轮改动无关） |
| Windows eval（`reports/eval.txt`） | `18/18 probes passed` |
| Ubuntu eval（`reports/eval.txt`） | `18/18 probes passed` |
| 结论 | 双平台 success，pytest 与 eval 结果与本报告 §1/§3.2 一致——验收标准满足 |

复核提到的"主要提交"（`9a935d4`）之外的上一轮 run（`41276159252`）与本轮 run 均为双平台 success。本轮验收以 Run `41280497556` 为准。

## 6a. 恢复原语的边界声明（第 5 轮复核 §4）

`_restore_no_clobber` 的 `os.link()` + `os.unlink()` 是 create-if-absent 的 no-clobber 恢复，但**不是**覆盖任意外部进程所有并发操作的完整原子事务：外部进程在两个目录项操作之间删除/替换目标路径，仍可能造成恢复现场的不确定性（该窗口不产生"覆盖外部内容"，但可能残留冗余目录项或使"放回"不可判定）。对遵循同一锁协议的 Jinlin 会话之间，workspace 写锁仍是硬保证。该边界已写入安全模型 `docs/REPORT-AND-HANDBOOK.md` §16 第 11 条。

---

## 7. 审查清单（可直接勾选）

- [ ] `git checkout review/followup-fixes` 后 `python -m pytest -q` = 76 passed
- [ ] `jinlin-agent eval --suite all` = 18/18
- [ ] `_restore_no_clobber`：只使用 `os.link` create-if-absent，无覆盖式 rename fallback
- [ ] Add / Update 回滚 mismatch、Update 恢复写失败、Delete 回滚恢复、Delete move-hash-mismatch 五个入口全部经 helper
- [ ] 三个竞争注入测试：路径重建内容完整、trash/backup 留存、`RECOVERY_UNCERTAIN`
- [ ] `events.py` 严格 UTF-8；非法字节测试双路径 fail-closed
- [ ] P2-2 / P2-3 确实未动
- [ ] CI 双平台 success；`main` 未被改动（仍为 `85854f1`）

---

## 9. 最终验收与合并记录

| 项 | 值 |
|---|---|
| 第 5 轮复核决定 | **Approve**，批准 `review/followup-fixes` fast-forward 合并到 `main` |
| 复核确认 | P1-1 / P1-2 / 五个恢复入口统一 / 三组竞争注入测试 / P2-1 全部通过；CI Run `41280497556` 双平台 success；`f1b0cb8 → 545e5cb` 仅文档差异 |
| 合并前条件 | §2.3 措辞修订（含测试断言消息对齐，仅提示文本，断言逻辑未变）——已随本系列提交完成 |
| 合并方式 | `git checkout main && git merge --ff-only review/followup-fixes && git push origin main`（无 squash / rebase） |
| 合并记录 | **已完成**：`main` 自 `85854f1` fast-forward 至合并点 **`414e826`**；main CI Run <https://github.com/hejinlin0824/jinlin-agent/actions/runs/41286417603> 双平台 **success**（Windows pytest 76/0/0/0、Ubuntu 76 tests / 16 平台守卫 skip、双平台 eval 18/18，与本文件 §6 同口径）；分支同名提交的 CI 亦双平台 success |
| 代码基线 | 代码内容自 `f1b0cb8` 起未再变化；合并点之后仅存在本节这类记录类纯文档提交（见 `git log --oneline`） |
| 证据保留 | 本文件 §6（Run `41280497556` 双平台 pytest/eval 数字）、§6a（恢复原语边界）、§2.3（措辞更正）随仓库归档 |
| 后续排期 | P2-2（日志 session_id 与实例比对）、P2-3（失败清理文件身份校验）、§2.3 建议的注入变体、P2-4…P2-8（同复核 §4 清单） |

---

## 8. 相关材料

- 本轮复核：《第 3 轮整改复核意见》（P1-1 / P1-2 / P2-1/2/3）
- 最终复核：《第 4 轮整改独立复核回复报告》（Approve）
- 历史整改记录：`docs/REVIEW-RESPONSE.md`（v1.3 + §0b）、`docs/REVIEW-SUBMISSION-ROUND3.md`
- 安全模型：`docs/REPORT-AND-HANDBOOK.md` §9、§16
- handoff 契约：`AGENT.md`
