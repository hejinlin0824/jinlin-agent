# P2 安全加固提交报告（第二阶段 · 第 1 批，供独立审查）

| 项 | 值 |
|---|---|
| 仓库 | `hejinlin0824/jinlin-agent` |
| 提交分支 | `review/followup-fixes`（**已与 `main` 同步至 `249bbab`**） |
| 归档状态 | 最新归档 HEAD = **`249bbab`**；`dad2dda` 是本批**最后一个代码提交**；其后 `ebc522b` 之前为本批文档，`249bbab` 为归档记录——`249bbab` 之后的提交如有，均为纯文档并单独标注，不混淆代码 HEAD 与文档 HEAD |
| 代码 HEAD | `dad2dda`（`a9c8d34` 主修复 + `dad2dda` 身份元组修正；代码内容自此冻结） |
| 对应复核 | 《第 6 轮独立复核意见》（P1 验收通过 + 新发现 source 清理竞态）+ 既有排期 P2-2 / P2-3 |
| 提交日期 | 2026-11-01 |
| 提交范围 | 第 6 轮建议的 P2 安全加固三件套 + 恢复现场注入强化（source 替换时序）；不含 P3 项 |

---

## 1. 结论摘要

| 复核排期项 | 状态 | 证据 |
|---|---|---|
| **source 清理竞态**（`os.link` 与 `os.unlink` 之间源路径被替换 → 按名误删外部新文件） | **已修复（复核方案 A）** | `_restore_no_clobber` 成功后**不再 unlink source**；冗余目录项由 `_rollback_transaction` 收集为 `redundant_trash` 并写入错误详情，标注为"检查后可安全移除"，恢复路径内不存在任何按名删除。测试：`test_restore_no_clobber_does_not_unlink_recreated_source` |
| **P2-2** EventLog 实例级 session_id 校验 | **已修复** | `EventLog.open()` 在修复/追加前校验全部已解析事件的 `session_id`：非空且 ≠ 实例预期 → `SESSION_CORRUPTED`（外 会话日志不再被静默接受并续写）；失败时照常释放锁，属主会话可立即打开自己的日志。测试：`test_open_rejects_mismatched_session_id` |
| **P2-3** `_atomic_create_new` 失败清理按路径 unlink | **已修复** | 创建后即从 fd 捕获 `(st_dev, st_ino)` 身份；写失败时**只有**路径仍可证明持有该 inode 才清理，路径被替换/消失 → 保留现场并抛 `RECOVERY_UNCERTAIN`。测试：`test_atomic_create_failure_does_not_unlink_replaced_path`（替换时序）、`test_atomic_create_failure_cleans_own_partial_file`（身份匹配分支仍清理自己的半写文件） |
| **恢复现场注入强化**（复核 §7："补齐 source 替换、目标替换等时序"） | **部分完成** | source 替换时序已补（上表第 1 行测试，直接单测 helper）；目标替换时序已有第 4 轮的三组注入测试覆盖。trash/backup 保留**本事务原始字节**、移动后再重建路径的变体仍未实施（维持第 5 轮"非阻塞"定位，见 §4） |

当前验证：**80 passed**（42 基础 + 38 安全/完成门/事务回归）、**18/18 probes passed**、CI 双平台结果见 §6。

---

## 2. 修复设计

### 2.1 source 清理竞态：方案 A（保留源，延迟受控清理）

按复核倾向采纳方案 A。核心变化只有一处：

```text
旧：os.link(source, dest) 成功 → os.unlink(source)      # 按名删除，可误删 T3 重建的外部文件
新：os.link(source, dest) 成功 → 保留 source（冗余目录项）
```

- 复核场景的 T2/T3（外部删除并重建 source）发生后，新实现**什么都不删**：外部新文件留在原地，本事务数据同时存在于 dest——两侧都完好；
- 冗余目录项的管理（复核指出的方案 A 代价）：`_rollback_transaction` 把"内容已安全回到路径 / 已随回滚弃置"的 trash/backup 文件名收集为 **`redundant_trash`**（新增返回值，随 `RECOVERY_UNCERTAIN` / 失败详情的 `details.redundant_trash` 上报）。这些文件：① 名字带 uuid 不可猜测；② 内容或已回到原路径、或是被回滚弃置的旧内容；③ 报告明确其语义为"检查后可移除"，由受控清理（后续版本）或人工删除，**恢复路径内部没有任何按名 unlink**；
- 所有权失配且放回被占用时，trash/backup **不计入** redundant——它仍是移动时刻内容的唯一可恢复副本，继续以 `rollback_incomplete` 语义保留。

### 2.2 P2-2：实例级 session_id 校验

`EventLog._repair_tail_before_append`（持锁、在修复与追加之前）逐事件检查：`session_id` 非空且 ≠ `self.session_id` → `SESSION_CORRUPTED`（"event log belongs to session X, not Y"）。省略 `session_id` 的事件维持读取器的既有容忍（兼容既有 fixture 与早期日志）；换行终止坏 JSON、torn tail 等原语义不变。`open()` 的 `except` 路径照常释放锁，属主会话可立即重新打开（测试已断言）。

### 2.3 P2-3：身份守卫的失败清理

`_atomic_create_new` 在写入落地后（`fsync` 之前）从 fd 捕获身份四元组 `(st_dev, st_ino, st_size, st_mtime_ns)`。写失败时：

| 清理时路径状态 | 行为 |
|---|---|
| `os.stat(path)` 身份 == 创建时身份 | 仍是我们自己的文件 → unlink 清理，原始异常照常抛出 |
| 身份不同（被并发替换）或路径已消失、或失败发生在身份捕获之前（写入中途） | **不删除**，抛 `RECOVERY_UNCERTAIN`（"left in place for inspection"），原异常挂为 `__cause__` |

**为什么是四元组而不是 `(dev, ino)`**：`dad2dda` 修正——首批实现（`a9c8d34`）只用 `(st_dev, st_ino)`，Ubuntu CI 立刻抓到真实缺陷：Linux 文件系统（ext4/overlayfs）会把刚释放的 inode 立刻分配给下一个 create，并发替换因此能呈现**完全相同的 (dev, ino)**，身份检查误判为自己的文件并按名删除。加入 `st_size` 与 `st_mtime_ns` 后，inode 复用的替换会因大小与纳秒时间戳不同而被识别（Windows/NTFS 的 file id 天然不复用，故首批本地与 Windows CI 未暴露）。这是 CI 跨平台矩阵价值的直接例证。

残余窗口如实声明：stat 与 unlink 之间的微秒级替换、以及"替换者刻意复制相同 size/mtime"的对抗场景仍是 best-effort（后者超出威胁模型），与 §16 的边界一致——本修复消除的是"按名删除"与"inode 复用误判"两个缺陷类，不声称零竞态。Windows/POSIX 的 `st_ino` 均由 CPython 提供（NTFS file id / inode），双平台可用。

---

## 3. 变更范围与验证

### 3.1 Diffstat（`f7c008b..dad2dda`，含两笔代码提交与本报告）

```
 docs/REVIEW-SUBMISSION-P2-HARDENING.md | 126 +++++  (本报告)
 src/jinlin_agent/events.py             |  16 +     (P2-2 实例级 session_id 校验)
 src/jinlin_agent/tools/patch.py        | 111 +++--- (方案 A + P2-3 身份守卫 + 四元组修正)
 tests/test_security.py                 | 113 +++++  (4 条新回归)
 7 files changed, 359 insertions(+), 48 deletions(-)
```

提交：`a9c8d34`（主修复）→ `ebc522b`（本报告初版）→ `dad2dda`（Ubuntu CI 抓到的 inode 复用误判修正）。另有归档类文档提交（`REVIEW-RESPONSE.md` §0c、手册 §9/§16、`AGENT.md`），不触碰 `src/` 与 `tests/`。

### 3.2 测试增量（76 → 80，全部先红后绿）

| 新测试 | 覆盖 |
|---|---|
| `test_restore_no_clobber_does_not_unlink_recreated_source` | source 替换竞态：helper 不 unlink（方案 A）；dest 内容正确；source 冗余项完整留存 |
| `test_open_rejects_mismatched_session_id` | 外会话日志拒开 + 锁释放 + 属主会话可正常打开续写 |
| `test_atomic_create_failure_does_not_unlink_replaced_path` | 失败清理遇到被替换路径：不删 + `RECOVERY_UNCERTAIN` + 外部内容留存 |
| `test_atomic_create_failure_cleans_own_partial_file` | 身份匹配分支：仍清理自己的半写文件并抛原始异常 |

### 3.3 验证记录（本机）

```
环境：Windows 11，Python 3.13.15
python -m pytest -q          -> 80 passed
jinlin-agent eval --suite all -> 18/18 probes passed
```

---

## 4. 未完成 / 后续排期（与复核清单一致）

| 项 | 定位 |
|---|---|
| trash/backup 保留本事务原始字节的注入变体（"移动后再重建路径"之外的另一半时序） | 维持非阻塞强化项 |
| 冗余 trash 的受控清理流程（如 `doctor` 子命令 / 会话开启时带锁清扫） | 方案 A 的后续配套，未实施 |
| Python 3.11/3.12 CI 矩阵、30–50 真实任务评测、覆盖率基线 | P3，未动 |
| 复核确认已关闭：第 4 轮两个 P1、报告 §1 措辞 | 无遗留 |

---

## 5. 审查清单（可直接勾选）

- [ ] `python -m pytest -q` = 80 passed；`jinlin-agent eval --suite all` = 18/18
- [ ] `_restore_no_clobber`：成功路径无任何 unlink；docstring 声明方案 A
- [ ] `_rollback_transaction`：返回 `redundant`；失配被占用场景的 trash 不计入 redundant（仍是可恢复副本）
- [ ] `details.redundant_trash` 随失败结果上报
- [ ] `EventLog.open()`：外会话日志 `SESSION_CORRUPTED`；失败释放锁；属主会话可续写
- [ ] `_atomic_create_new`：身份匹配才清理；替换/消失 → `RECOVERY_UNCERTAIN`
- [ ] P3 项未动；代码自 `f1b0cb8` 的已验收语义（五个恢复入口 no-clobber）未回退

---

## 6. CI

- Workflow：`.github/workflows/ci.yml`，矩阵 `windows-latest` / `ubuntu-latest`，Python 3.13；
- 首批代码提交 `a9c8d34` 的 run：Ubuntu **failure**（<https://github.com/hejinlin0824/jinlin-agent/actions/runs/41289041280>，`test_atomic_create_failure_does_not_unlink_replaced_path` 暴露 inode 复用误判，见 §2.3）、Windows success——CI 抓到真实缺陷；
- 修正提交 `dad2dda` 的 run：Windows + Ubuntu 均 **success**（<https://github.com/hejinlin0824/jinlin-agent/actions/runs/41289620436>）；
- 判定标准与之前一致：两 job 均 success，pytest/eval 数字与 §3.3 同口径（Ubuntu 的 16 个 skip 为 Windows 专属 L1 沙箱测试的平台守卫）。

---

## 7. 相关材料

- 第 6 轮复核：《第 6 轮独立复核意见》（source 清理竞态 + 下一阶段工作顺序）
- 历史整改记录：`docs/REVIEW-RESPONSE.md`（§0c 记录本批）、`docs/REVIEW-SUBMISSION-ROUND4.md`（§9 已归档）
- 安全模型：`docs/REPORT-AND-HANDBOOK.md` §9、§16
- handoff 契约：`AGENT.md`
