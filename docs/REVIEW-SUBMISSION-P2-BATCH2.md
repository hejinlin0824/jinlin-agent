# P2 安全加固第 2 批整改报告（事务清理生命周期，供独立审查）

| 项 | 值 |
|---|---|
| 仓库 | `hejinlin0824/jinlin-agent` |
| 基线 | `main` = `249bbab`（第 1 批归档线；本批**未合并 main**，按复核 §五 等待复核批准） |
| 分支 | `review/followup-fixes` |
| 本批提交 | `7c246d8`（归档状态修正，纯文档）→ `d600c9d`（P0）→ `6b4ef0e`（P2 legacy）→ `48bdb87`（P1）→ `1359a90`（P3）→ 本报告（纯文档） |
| 最终代码 HEAD | `1359a90`（其后仅本报告等文档提交，会单独标注） |
| 对应复核 | 《第二阶段 P2 安全加固第 1 批复核回复》（§三 顺序：P0 → P1 → P2 → P3） |
| 本机验证 | `python -m pytest -q` → **95 passed**（80 基线 + 15 新增）；`jinlin-agent eval --suite all` → **18/18** |
| CI | 分支推送后双平台结果见 §7（判定：两 job 均 success 才提交报告） |

---

## 1. 结论摘要（对照复核 §三）

| 项 | 状态 | 实现 | 测试 |
|---|---|---|---|
| **P0** Delete backup 正常提交后的清理竞态 | **已修复** | 移动验证后即捕获 backup 指纹 `(dev, ino, size, mtime_ns)`；提交后清理**只在指纹仍匹配时** unlink；不匹配 → 保留并报 `extra["cleanup_deferred"]` + 输出 note；消失 → 无事发生；**清理失败不改变已提交事务的成功状态** | `test_delete_backup_cleanup_skips_replaced_backup`（清理期 stat 注入替换）、`test_delete_backup_cleanup_failure_is_reported_not_fatal`（unlink 失败）、`test_delete_backup_vanished_before_cleanup_is_not_an_error`（消失，钉住既有正确行为） |
| **P1** 冗余 trash/backup 受控清理机制 | **已实现** | 新模块 `trash.py`：事务创建 trash/backup 时同步写清单 `<ws>/.jinlin/trash-manifest.jsonl`（事务记录 = 归属证明）；`jinlin-agent clean [--workspace W] [--apply]`：**默认 dry-run** 列出可清理/不认识两类；`--apply` 在 workspace 写锁下只删"严格命名模式 + 清单记录一致"的文件；单文件失败保留清单条目（可重复执行收敛）；不认识的名字**只报告不删除** | `tests/test_trash.py` 7 条：清单证明要求、dry-run 默认、apply 删证明保不认识、失败保留条目且可重复、写锁拒 绝、torn 清单尾容忍、patch→清单→clean 集成链路 |
| **P2** EventLog legacy session_id 兼容策略 | **已实现（fail-closed）** | 解析器强制三类：① 全部无 session_id = legacy（容忍，唯一合法的无归属形态）；② 从首个带 session_id 的事件起全部一致（严格，含实例比对）；③ **归属事件之后再出现无归属事件** = 本写入器不可能产生的形状 → `SESSION_CORRUPTED`（读取与 open 双路径）。外会话日志维持拒绝。迁移规则明确化：对 legacy 日志追加即产生"无归属前缀 + 归属后缀"，是唯一被接受的混合形状 | `test_mixed_log_attributed_then_unattributed_is_rejected`（旧实现静默接受 → 红）、`test_legacy_log_without_session_id_migrates_on_append`（迁移路径钉死：追加带新 id、读取为 [1,2]） |
| **P3** `_atomic_create_new` 边界补强测试 | **已补** | 三条边界钉死（见 §5），其中同 size + inode 复用场景直接守卫 `dad2dda` 的 mtime 分量 | `test_atomic_create_failure_replaced_same_size_is_not_unlinked`、`test_atomic_create_failure_fingerprint_collision_documents_residual`、`test_atomic_create_failure_vanished_path_is_reported_uncertain` |

## 2. 逐项根因与修复

### 2.1 P0：提交后 Delete backup 清理

- **根因**：`applied` 事务提交后的清理循环 `record["backup"].unlink()` 按路径名删除；backup 移动完成到提交清理之间，外部进程删除并重建 backup 路径时，重建文件会被误删。
- **修复**：`os.replace` 移动并校验哈希后立即 `os.stat(backup)` 捕获指纹并存入事务记录；提交清理时重新 stat 并**四元组比对**（与 P2-3 的 inode 复用教训一致），匹配才 unlink（成功后 `forget_trash` 摘除清单条目）；不匹配/消失/unlink 失败 → backup 留存、名字进 `extra["cleanup_deferred"]` 与输出 note。清单条目使延迟清理仍可被 `clean` 子命令接管（P1 联动）。
- **边界（如实声明）**：指纹比对（stat）与 unlink 之间仍有微秒级窗口，且复制完整指纹的对抗者在威胁模型之外——与手册 §16 的声明一致，不声称原子身份删除。
- **影响范围**：仅成功提交后的清理分支；`RECOVERY_UNCERTAIN` 回滚路径、写前校验、P1-1/P1-2 已验收语义均未触碰。

### 2.2 P1：受控清理（`trash.py` + `clean` 子命令）

- **归属证明**：模式匹配 `^\.<orig>\.jinlin-(rollback|txn)-[0-9a-f]{8}$` **且** 清单 `<ws>/.jinlin/trash-manifest.jsonl` 记录同名同类——清单条目由创建该文件的事务写入（写入点：回滚 trash 两处、Delete backup 一处），是事务记录而非名字猜测。清单自身损坏（torn 尾行）按"无证明"处理。
- **协议**：`clean` 持 workspace 写锁（非阻塞；被占 → `status: locked` 拒绝）；默认 dry-run；`--apply` 逐文件 unlink，失败保留清单条目并进 `failed` 报告（可重复执行收敛）；`unrecognized` 只报告。成功删除的条目从清单摘除。
- **联动**：P0 延迟清理的 backup 因创建时已入清单，自动成为可受控清理对象。
- **影响范围**：新增模块与子命令；`patch.py` 只加了创建点记录与成功 forget，不改任何已验收语义。

### 2.3 P2：legacy session_id 三类策略

- **根因**：解析器只校验"出现过的 session_id 互相一致"，导致 `归属事件之后再出现无归属事件` 的混合形状被静默接受——本写入器不可能产生该形状。
- **修复**：解析器跟踪归属状态，归属事件后出现无归属事件 → `SESSION_CORRUPTED`。三类日志行为：全无归属（legacy）容忍；全归属一致严格（含 `open()` 实例比对）；混合仅接受"无归属前缀 + 同一会话归属后缀"这一显式迁移形状（恰为对 legacy 日志追加的实际产物），其余混合形状拒绝。历史日志内容从不被改写（迁移只在追加时发生）。
- **影响范围**：`events.py::_parse_log_data` 一处；`read_events`/`open` 共用该解析器，行为自动一致。

### 2.4 P3：边界矩阵（纯测试）

| 测试 | 时序 | 结果 |
|---|---|---|
| `..._replaced_same_size_is_not_unlinked` | 清理期替换：同 size（17B）+ inode 复用，mtime 不同 | 替换存活 + `RECOVERY_UNCERTAIN`（守卫 `dad2dda` 的 mtime 分量） |
| `..._fingerprint_collision_documents_residual` | 替换者复制完整指纹（size + mtime_ns） | 被当作自身文件删除 + 原始 OSError——**钉死真实边界**：指纹碰撞在威胁模型之外 |
| `..._vanished_path_is_reported_uncertain` | 检查时路径已消失（FileNotFoundError） | `RECOVERY_UNCERTAIN`，无按名删除 |

## 3. 关键代码差异（`249bbab..1359a90`）

```
 src/jinlin_agent/cli.py             |  +27   (clean 子命令)
 src/jinlin_agent/events.py          |  +21   (归属形状规则)
 src/jinlin_agent/tools/patch.py     |  +30   (backup 指纹守卫清理 + 清单记录/forget)
 src/jinlin_agent/trash.py           | +165   (新模块：清单/扫描/受控清理)
 tests/test_security.py              | +116   (P0 三条 + P3 三条 + P2 两条)
 tests/test_trash.py                 | +153   (新文件：P1 七条)
 7 files changed(含文档), 约 +430/-20
```

## 4. 旧实现失败证据（TDD）

| 测试 | 旧实现（`249bbab`）行为 | 新实现 |
|---|---|---|
| `test_delete_backup_cleanup_skips_replaced_backup` | cleanup 按名 unlink 删除替换内容；`extra` 无 `cleanup_deferred` | **红→绿** |
| `test_delete_backup_cleanup_failure_is_reported_not_fatal` | unlink 失败被 `except OSError: pass` 静默吞掉 | **红→绿** |
| `test_mixed_log_attributed_then_unattributed_is_rejected` | 解析器接受混合形状 | **红→绿** |
| `test_patch_records_trash_in_manifest` | 无清单机制 | **红→绿** |
| `test_scan_requires_manifest_proof` 等 6 条 | `trash.py` 不存在 | **红→绿** |
| `test_atomic_create_failure_replaced_same_size_is_not_unlinked` | `a9c8d34`（二元组身份）会误判删除；现实现靠 mtime 分量通过 | 现绿；对旧身份红 |
| 钉死类（vanished backup、legacy 迁移、指纹碰撞、own-partial） | 记录既有正确行为/已声明边界 | 通过 |

## 5. 残余竞态与威胁模型（如实声明）

1. **stat→unlink 微秒窗口**（P0 提交清理、P2-3 创建清理、P1 clean）：比对通过后、删除执行前，替换者仍可写入；复制完整指纹（size+mtime_ns）的对抗者在威胁模型之外（`..._fingerprint_collision_documents_residual` 钉死该边界）。缓解：workspace 写锁消除 Jinlin 会话间竞争 + uuid 文件名不可猜测 + 失败保留现场。
2. **link+unlink 非完整原子事务**（第 6 轮已确认）：source 冗余项保留不删，残余为"冗余目录项/放回不可判定"，不再是数据覆盖或误删。
3. **清单可被外部进程篡改**：能写 workspace 的外部进程可伪造清单条目诱导 `clean --apply` 删除其伪装文件——但该进程本就拥有写权限，无权限提升；清单损坏按无证明处理（report-only）。
4. **legacy 无归属日志**：归属无法证明是历史事实；策略为"容忍读取 + 追加即显式迁移"，未篡改历史。

以上均为 best-effort 边界描述，不声称抵御任意外部进程的全部并发操作。

## 6. 遗留 trash / backup 说明

正常流程（提交成功 + 清理成功、回滚成功）不再产生遗留；以下情况会留下 dotfile，均为有意保留：① 回滚冗余项（方案 A，已入清单）；② 失配被占用的 trash（唯一可恢复副本）；③ 提交后清理延迟的 backup（已入清单）。均可由 `jinlin-agent clean` 列出并在确认后 `--apply` 移除。

## 7. CI

- 首推（含 P0/P2 代码）与末推（完整批）的 run：分支 Actions 页；判定标准：Windows + Ubuntu 两 job 均 success（pytest 95 passed / 平台守卫 skip 说明不变，eval 18/18）。报告提交前已确认本批全部提交双平台绿。

## 8. 关闭与排期

| 已关闭 | 仍排期 |
|---|---|
| 第 4 轮 P1×2（已验收合并）；第 6 轮 source 清理竞态；P2-2 实例校验；P2-3 身份清理；本批 P0/P1/P2/P3 | 同 size+同 mtime 的原子身份删除（平台 API 不支持，保守策略已声明）；`clean` 并入 `doctor` 的会话开启自动清扫（可选）；P3 大项：Python 3.11/3.12 矩阵、30–50 真实任务评测、覆盖率基线 |

## 9. 审查清单

- [ ] `python -m pytest -q` = 95 passed（Windows 全量；Ubuntu 另有平台守卫 skip）；`eval` 18/18
- [ ] P0：提交清理四元组守卫；`cleanup_deferred` 上报；事务成功与清理成功分离
- [ ] P1：清单证明归属；dry-run 默认；写锁协议；失败保留条目可重复；unrecognized 只报告
- [ ] P2：三类日志策略；混合形状（归属后无归属）双路径拒绝；legacy 迁移有测试
- [ ] P3：三条边界测试与 §5 声明一致；指纹碰撞被钉为 documented residual 而非"已解决"
- [ ] `main` 仍在 `249bbab`（本批未合并，等复核批准）
