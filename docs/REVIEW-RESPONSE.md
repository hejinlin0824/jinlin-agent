# 独立评审响应与整改记录（v1.8）

| 项 | 值 |
|---|---|
| 评审来源 | 首轮～第 8 轮独立评审与复核（详见 §0a–§0e 各表） |
| 评审基线 | `f89384c`（首轮）→ `85854f1`（第 1–4 轮合并基线）→ `f7c008b`（第 4 轮 P1 验收线）→ `249bbab`（第 1 批 P2 加固归档线） |
| 本响应版本 | v1.7：第 2 批 P2 加固的第 8 轮复核整改（P0 同对象哈希+指纹 / P1 manifest 路径绑定+原子重写+降级诊断 / P2 null 与空串拒绝 / P3 四字段验证） |
| 结论 | 第 4 轮 P1 已验收合并；第 1 批 P2 加固已归档；第 2 批暂不合并，按第 8 轮复核意见追加修复完毕（§0e），等待再次复核 |

## 0f. 第 10 轮复核整改（v1.8，第二阶段第 2 批续）

第 10 轮复核确认 move→open 窗口修复有效，指出：rescue 未实现（P0 阻断）、clean 的 stat→unlink 竞态（P1 阻断）、clean 忽略 forget 失败（部分完成）。本轮全部完成（代码 `17b2262`）：rescue 文件机制（唯一命名排他创建 + 短写安全循环 + fsync + 逐字节验证 + 清单归属 + `rescued_files` 上报）；clean 改为隔离区移动后验证（被扫描路径永不被 unlink，不匹配对象无覆盖放回并报告）；forget 失败计入 `manifest_degraded`。详见 `docs/REVIEW-SUBMISSION-P2-BATCH2-R2.md` §9。

## 0e. 第 8 轮复核整改（v1.7，追加于第 2 批之上）

第 8 轮复核：P0 事务状态分离、EventLog 迁移规则、边界测试获认可；两个阻断项（P0 backup 指纹捕获时序、P1 manifest basename 绑定）与配套项需先修复。全部完成，代码提交 `1c3e594` / `a98741a` / `5c69e34` / `346561d`，`main` 保持 `249bbab` 不变。逐项表见 `docs/REVIEW-SUBMISSION-P2-BATCH2.md` §8a；摘要：

| 复核项 | 处理 | 测试 |
|---|---|---|
| P0 backup 哈希读取与身份捕获为两次独立路径访问 | 打开一次 backup：sha256 经同一 fd 读取、指纹经 fstat(同一 fd) 获取，两者绑定同一文件对象；提交清理区分 `FileNotFoundError`（无需清理）与其他 `OSError`（保留 + `cleanup_deferred` 附原因） | `test_delete_backup_identity_captured_from_same_object_as_hash`（红→绿）、`test_delete_backup_cleanup_stat_error_is_reported_as_deferred`（红→绿） |
| P1 manifest 按 basename 认定归属（跨目录同名可被误删） | 清单记录并校验完整 workspace 相对路径 + kind + orig 对应（按原文件 basename 比对）；删除前对新鲜清单重核验；`forget` 按路径寻址，同名不同目录记录完全独立 | `test_clean_same_basename_in_subdirectory_is_not_deleted`、`test_clean_same_basename_manifest_records_are_independent`（红→绿） |
| P1 清单重写非原子 / record 失败无诊断 | 临时文件 + fsync + `os.replace` 原子重写（失败原记录完整）；`record_trash` 返回成功与否，失败时事务照常但 `trash_manifest_degraded` 随结果上报 + 输出 note，该类遗留文件只能报告、`clean --apply` 不删 | `test_forget_failure_keeps_manifest_intact`、`test_manifest_write_failure_degrades_to_report_only`（红→绿） |
| P2 显式 null / 空 session_id 混入 legacy 规则 | 仅字段缺失进入 legacy 规则；存在但非非空字符串 → `SESSION_CORRUPTED`（读取与 open 双路径） | `test_event_with_explicit_null_session_id_is_rejected`、`test_event_with_empty_session_id_is_rejected`（红→绿） |
| P3 指纹碰撞测试未验证四字段全等 | 重写为显式测量 (dev, ino, size, mtime_ns) 四分量碰撞：全等平台（Linux）钉死 documented residual；不复用平台（Windows）断言替换存活 + `RECOVERY_UNCERTAIN`——结论不超出实际验证 | `test_atomic_create_failure_fingerprint_collision_documents_residual`（重写） |

## 0d. 第二阶段 P2 安全加固第 2 批（v1.6，事务清理生命周期）

第 1 批复核批准归档，并给出本批顺序：P0（Delete backup 提交清理竞态）→ P1（受控清理机制）→ P2（legacy session_id 策略）→ P3（边界测试补强）。全部完成，代码提交 `d600c9d` / `6b4ef0e` / `48bdb87` / `1359a90`；**按复核要求未合并 main**，详见 `docs/REVIEW-SUBMISSION-P2-BATCH2.md`。

| 项 | 处理 | 回归测试 |
|---|---|---|
| **P0** 提交后 Delete backup 按名 unlink 可误删外部重建文件 | 移动验证后捕获 backup 四元组指纹；提交清理仅指纹匹配才删除；不匹配/消失/unlink 失败 → 留存 + `extra["cleanup_deferred"]` + 输出 note；**清理失败不改变已提交事务状态** | `test_delete_backup_cleanup_skips_replaced_backup`（清理期 stat 注入替换）、`..._failure_is_reported_not_fatal`（unlink 失败诊断）、`..._vanished_before_cleanup_is_not_an_error`（消失） |
| **P1** 冗余 trash/backup 受控清理 | 新模块 `trash.py`：创建时写事务清单（归属证明 = 严格命名模式 + 清单记录一致）；`jinlin-agent clean` 子命令默认 **dry-run**，`--apply` 持 workspace 写锁只删可证明文件；失败保留清单条目可重复执行；不认识的只报告不删除；与 P0 延迟清理联动 | `tests/test_trash.py` 7 条（清单证明、dry-run 默认、apply 语义、失败可重复、锁拒绝、torn 清单、patch→清单→clean 集成） |
| **P2** legacy session_id 兼容策略 | 解析器强制三类：全无归属（legacy 容忍）/ 全归属一致（严格）/ 显式迁移形状（无归属前缀 + 单会话归属后缀）；归属后再无归属 → `SESSION_CORRUPTED`；外会话维持拒绝；历史内容不改写 | `test_mixed_log_attributed_then_unattributed_is_rejected`（旧实现静默接受→红）、`test_legacy_log_without_session_id_migrates_on_append`（迁移路径钉死） |
| **P3** `_atomic_create_new` 边界矩阵 | 三条注入：同 size+inode 复用替换（mtime 区分，存活）、完整指纹碰撞（威胁模型外，钉死 documented residual）、检查时路径消失（`RECOVERY_UNCERTAIN`） | `test_atomic_create_failure_replaced_same_size_is_not_unlinked`、`..._fingerprint_collision_documents_residual`、`..._vanished_path_is_reported_uncertain` |

## 0c. 第二阶段 P2 安全加固第 1 批（v1.5，基于《第 6 轮独立复核意见》+ 既有排期）

第 6 轮复核**验收通过**第 4 轮两个 P1（合并已完成、main CI 双平台绿），同时新发现 `_restore_no_clobber` 的源路径清理竞态，并给出下一阶段顺序：先 source 竞态，再 P2-2、P2-3。本批按该顺序全部完成（方案 A），代码提交 `a9c8d34`。

| 项 | 处理 | 回归测试 |
|---|---|---|
| **source 清理竞态**（link→unlink 之间源路径被外部替换，按名 unlink 误删外部新文件） | 采纳复核**方案 A**：`_restore_no_clobber` 成功后**不删除 source**；冗余目录项由 `_rollback_transaction` 收集为 `redundant_trash`（新增第三返回值）随失败详情上报，语义为"检查后可移除"；失配且放回被占用时 trash 不计入 redundant（仍是可恢复副本）。恢复路径内不存在任何按名 unlink | `test_restore_no_clobber_does_not_unlink_recreated_source`（link 后注入 source 替换：外部新文件留存、dest 内容正确、helper 不调用 unlink） |
| **P2-2** EventLog 实例级 session_id 校验 | `open()` 在修复/追加前校验每个已解析事件的 `session_id`：非空且 ≠ 实例预期 → `SESSION_CORRUPTED`；省略 session_id 的事件维持既有容忍；失败释放锁 | `test_open_rejects_mismatched_session_id`（外会话日志拒开 + 锁释放 + 属主会话可打开续写 seq=2） |
| **P2-3** `_atomic_create_new` 失败清理按路径 unlink | 从 fd 捕获 `(st_dev, st_ino)` 身份；写失败时仅当路径仍可证明持有该 inode 才清理；被替换/消失 → 保留现场抛 `RECOVERY_UNCERTAIN`（原异常为 `__cause__`） | `test_atomic_create_failure_does_not_unlink_replaced_path`（清理期路径被替换：外部内容留存 + `RECOVERY_UNCERTAIN`）、`test_atomic_create_failure_cleans_own_partial_file`（身份匹配：仍清理自身半写文件） |

残余窗口声明：stat 与 unlink 之间的微秒级替换仍是 best-effort（复核已指出 stat 检查不构成完整方案，故本批同时按方案 A 消除了恢复路径内的按名删除）；已在 `REPORT-AND-HANDBOOK.md` §16 如实声明。

## 0b. 第 4 轮复核整改（v1.4，基于《第 3 轮整改复核意见》）

第 4 轮结论为 **Request changes**：上一轮全部整改项通过；新发现恢复路径的残留竞争（P1×2）。本轮按复核 §6 要求只处理 P1-1 / P1-2，并顺手补上复核明确建议的 P2-1；**P2-2 / P2-3 未动，单独排期**。代码提交 `f1b0cb8`，`main` 保持 `85854f1` 不变。

| 复核项 | 处理 | 回归测试 |
|---|---|---|
| **P1-1** 回滚恢复用 `os.replace(trash, path)`，"trash 已移动、原路径被外部重建"时可覆盖外部文件 | 新增统一 `_restore_no_clobber(source, dest)` helper：`os.link` 原子 create-if-absent 放回 + 删除冗余 source 名；dest 被占用或文件系统不支持硬链接 → 返回 False，**双方保留**（外部内容在路径、本事务内容在 trash/backup）并报 `RECOVERY_UNCERTAIN`；**不以覆盖式 rename 作为静默回退**。Add / Update 回滚 mismatch 与 Update 恢复写失败后的放回全部改走 helper | `test_rollback_restore_is_no_clobber_when_path_recreated[add]`、`[update]`（参数化注入"路径重建"时序；断言外部内容完整、trash 留存、`RECOVERY_UNCERTAIN`） |
| **P1-2** Delete 写入阶段 move 后 hash 不匹配的恢复分支 `exists()` + `os.replace(backup, path)` 有同类窗口 | 同一 helper：恢复成功 → `FILE_CHANGED_EXTERNALLY`（original restored）；路径被占用 → `RECOVERY_UNCERTAIN` + `details.kept_aside`（备份文件名，原内容留存可恢复）。同时删除 Delete **回滚**恢复中"重查存在 + rename"的覆盖式 fallback | `test_delete_move_hash_mismatch_restore_is_no_clobber`（注入 move 前外部改写 + 放回前路径重建；断言外部内容、backup 留存、`RECOVERY_UNCERTAIN`） |
| **P2-1** 事件解码 `errors="replace"` 使非法 UTF-8 被静默替换（复核建议顺手补上） | 改为严格 `raw.decode("utf-8")`；非法字节触发 `UnicodeDecodeError` → 统一 `SESSION_CORRUPTED`（torn tail 判定不受影响：无换行尾片段仍按 torn write 截断） | `test_invalid_utf8_inside_event_is_rejected`（JSON 字符串值内 `\xff\xfe`；`read_events` 与 `EventLog.open()` 双路径 fail-closed） |
| P2-2 事件 `session_id` 未与 EventLog 实例预期比较 / P2-3 `_atomic_create_new` 失败清理按路径 unlink | **未动**（复核 §6：本轮不扩大范围，单独排期） | — |

## 0a. 第 3 轮复核整改（v1.3，基于 `JINLIN_AGENT_SUBMISSION_REVIEW.md`）

第 3 轮结论为 **Request changes**：L5 `exit_code=None` 与 CI 基础设施已通过；两个 P1 阻止合并。本轮全部按"先写失败测试、看它红、再实现"完成，全部工作仍在 `review/followup-fixes` 分支，`main` 保持 `85854f1` 不变。

| 复核项 | 处理 | 回归测试 |
|---|---|---|
| **P1-A** `create=True` 不是原子 create-only（`exists()` 检查后 `os.replace` 可覆盖并发创建者） | 新建路径改用 `os.open(O_CREAT\|O_EXCL)` **独占创建**（Windows/POSIX 均为文件系统级原子 no-clobber），不再经过 `os.replace`；竞争失败映射为 `PATCH_CONFLICT`；`atomic_write` docstring 删除"never overwrites"式过强表述，改为准确的 `O_EXCL` 语义 | `test_apply_patch_add_no_clobber_at_replace_window`（注入 `os.replace`：断言 Add 路径不再调用它）、`test_apply_patch_add_conflict_when_creator_wins_exclusive_create`（在独占创建前瞬间注入并发创建者：断言 `PATCH_CONFLICT` 且外部内容完好） |
| **P1-A** Update `expected_sha256` 检查后、`os.replace` 前的 TOCTOU | 按复核允许的方案**收缩承诺**：写前哈希复核只**缩小**（不消除）窗口；新增 `<workspace>/.jinlin/write.lock` 顾问写锁，写阶段串行化多个 Jinlin 会话（竞争方得到可重试的 `WORKSPACE_LOCKED`）；残余风险写入安全模型（`REPORT-AND-HANDBOOK.md` §16 第 11 条）；写后校验与 `RECOVERY_UNCERTAIN` 保留 | `test_apply_patch_update_toctou_after_hash_check`（注入最后窗口外部写入，钉住已收缩契约的实际行为）、`test_editing_tools_serialize_on_workspace_write_lock`（持锁会话阻塞第二个会话的写阶段，释放后同补丁成功） |
| **P1-A** 回滚自身 check-then-act（hash 检查后 unlink/replace 可破坏外部内容） | 回滚改为**先移动后校验**：先把当前内容原子改名到事务级 trash（内容保全），校验哈希归属后才删除/替换；不匹配则原样移回并报 `RECOVERY_UNCERTAIN`；Update 恢复改走 `create=True`（`O_EXCL`），外部内容抢到路径时保留双方内容；Delete 恢复改用 `os.link`（create-if-absent），不再覆盖并发重建的文件 | `test_rollback_after_check_preserves_external_content`（在 trash 改名前注入外部写入：断言外部内容原样保留 + `RECOVERY_UNCERTAIN`） |
| **P1-B** `EventLog.open()` 容忍 torn tail 但不修复，恢复追加会把日志永久写坏 | `open()` 取锁后先解析全文件（`_parse_log_data`，读取与修复共用同一套校验规则）：末尾**无换行且无法解析**的碎片 → 截断到最后一条完整事件并 `fsync`；最后一条**完整 JSON 缺换行** → 补写 `\n` 并 `fsync`；**换行终止的坏 JSON** → 仍 fail-closed 不自动修复；`open()` 校验失败时**释放会话锁** | `test_open_repairs_torn_tail_before_append`、`test_open_repairs_missing_trailing_newline`（open → append → reopen 双路径集成测试）、`test_open_failure_releases_session_lock`（损坏日志打开失败后第二个实例可立即取锁） |

不阻塞但已采纳的复核建议：无（Python 3.11/3.12 CI 矩阵、L5 失败计数比较仍按报告意见延后，见 §2）。

## 0. Follow-up 复核整改（v1.2，基于 `85854f1` 复核）

| 复核项 | 处理 | 回归测试 |
|---|---|---|
| **P1 多文件回滚误删/覆盖并发写入** | 重写事务写入：只登记**本事务成功产生**的变更，并保存写后哈希；回滚前核验当前哈希仍等于本事务写后状态，不一致则保留外部内容并返回 `RECOVERY_UNCERTAIN` + `rollback_incomplete`；Add 冲突绝不删除并发创建者文件；Delete 改为"原子重命名到备份 → 校验 → 事务成功才删除备份"，校验失败时恢复原文件 | `test_apply_patch_add_conflict_does_not_delete_external_file`、`test_apply_patch_update_conflict_does_not_restore_over_external_edit`、`test_apply_patch_rollback_only_reverts_own_mutations`、`test_apply_patch_delete_conflict_preserves_external_file` |
| **P1 L5 `exit_code=None` 可能放行** | 未知进程状态 fail-closed：`timed_out` / `exit_code is None` / 未知非零退出 / exit 0 但输出含失败 ID，均判 failed | `test_l5_none_exit_code_fails_closed` |
| **P2 事件尾部判定过宽** | 改为 bytes 读取并保留"是否以换行结束"：只有**无换行结尾**的最后片段允许作为 torn tail；换行终止的坏 JSON、中间空行、schema/session 不一致、seq 缺口一律 `SESSION_CORRUPTED` | `test_torn_tail_without_newline_is_tolerated`、`test_newline_terminated_corrupt_tail_is_rejected`、`test_blank_line_between_events_is_rejected`、`test_schema_and_session_mismatch_is_rejected` |
| **P2 Claim-Evidence 范围表述** | 明确限定：**测试类 Claim 已建立 L3 证据约束；通用 Claim-Evidence 语义匹配仍未实现**（后续用 `claim_type`/`evidence_type` 设计） | 文档修正 |
| 复核建议：CI 记录 | 新增 `.github/workflows/ci.yml`：Windows + Ubuntu、Python 3.13，跑 pytest（junitxml）+ 离线 eval，上传报告 artifact | CI |

说明：本轮按要求先推送到评审分支 `review/followup-fixes`，不直接改 `main`；由评审确认 diff 与 CI 结果后再合并。

## 1. 首轮已修复（含回归测试）

| 评审项 | 修复 | 位置 | 回归测试 |
|---|---|---|---|
| **P0-1** 父目录 symlink/Junction 越界（目标不存在时绕过） | 新增 `paths.resolve_in_workspace()`：词法边界 → realpath 边界（含 Junction）→ 禁用 follow 时逐级拒绝 link 组件；文件工具、补丁引擎、Shell 共用 | `src/jinlin_agent/paths.py`、`policy.py`、`tools/base.py` | `test_parent_junction_escape_rejected`、`test_dotdot_and_absolute_escape_rejected` |
| **P0-2** Shell `cwd` 未校验边界 | Policy 在命令分级前校验 cwd；Shell handler 经 `ToolContext.resolve` 二次防御 | `policy.py::_check_execute`、`tools/base.py` | `test_shell_cwd_outside_denied` |
| **P1-1** `write_file` 覆盖已存在文件时不检查读后写 | 已存在文件强制 `check_clean`（未读 → `FILE_NOT_READ`，被外部改 → `FILE_CHANGED_EXTERNALLY`）；新文件走 `create=True` | `tools/patch.py::write_file` | `test_write_file_existing_requires_read`、`test_write_file_external_change_rejected` |
| **P1-2** `atomic_write` 替换前不复核哈希 | 增加 `expected_sha256` 写前复核 + 写后校验；`create=True` 拒绝并发创建覆盖；多文件补丁的 add/update/delete 全部走同一守卫 | `tools/patch.py::atomic_write` | `test_atomic_write_hash_guard` |
| **P1-3** 证据"存在"不等于"能支撑 Claim" | 测试类 Claim 必须引用**通过的 L3 验证**（ver_*、artifact_*、"L3 tests"、测试命令），文件路径不再能背书"测试通过" | `verify.py::CompletionGate` | `test_gate_requires_passing_test_evidence_for_test_claims` |
| **P1-4** L5 只比较失败数量 | 解析失败/错误测试 **nodeid 集合**、退出码、timeout；比较集合差异；未知非零退出码不默认通过 | `verify.py::parse_test_output/_level5` | `test_l5_detects_changed_failure_identity`、`test_parse_test_output_extracts_failure_ids` |
| **P1-5** L0 安全 findings 不阻断完成 | L0 未运行或 failed → 完成门硬性拒绝 | `verify.py::CompletionGate` | `test_gate_rejects_failed_l0` |
| **P1-6** 基线异常被静默吞掉 | baseline 事件固定带 `status=passed/failed/skipped/error` + 原因；异常写事件并进入报告警告 | `session.py::_capture_baseline`、`verify.py::ensure_baseline` | 由 E2/E3/L5 测试覆盖 |
| **P2-1** 事件读取忽略任意位置损坏 | 只容忍**文件末尾 torn write**；中间损坏、`seq` 缺口、缺字段 → `SESSION_CORRUPTED` | `events.py::_iter_events_checked` | `test_mid_log_corruption_is_reported`、`test_seq_gap_is_reported`、`test_torn_tail_still_tolerated` |
| **P2-doc** README 与报告的真实模型验证矛盾 | README 删除过时"等待 Key"描述；真实模型验证以可核验记录为准；`<this-repo>` 占位符替换 | `README.md` | 文档评审 |

## 2. 部分修复 / 明确延后

| 评审项 | 状态 | 说明 |
|---|---|---|
| P1-2 多文件补丁崩溃原子性 | **部分** | 已有写前哈希复核、写后校验、异常回滚；**跨文件崩溃事务（transaction manifest）未实现**，进程被强杀仍可能部分替换 |
| P2-2 上下文最终 Token 预算闭环 | **部分** | 已有内容相关预算与压缩；最终 messages 的二次统一估算、required 超预算的显式 `BUDGET_EXCEEDED` 仍待做 |
| P2-3 脱敏覆盖嵌套结构/自由文本 | **部分** | 事件参数按顶层键脱敏；嵌套对象、自由文本、Artifact 统一 redaction 未实现 |
| P2-4 事件 hash chain | **延后** | 当前用"末尾容错 + 中间报错 + seq 连续性"保证完整性；hash chain 作为后续增强 |
| 独立真实任务评测（30–50 个） | **未做** | 需要外部任务集与统一预算，属于下一阶段工作 |
| 覆盖率/CI 记录 | **未做** | 当前只有 pytest 结果，无覆盖率与 CI 趋势 |

## 3. 安全边界现状（修复后）

```
所有文件入口 ──► paths.resolve_in_workspace()
                    │ 1. 词法边界（../、绝对路径）
                    │ 2. realpath 边界（symlink + Junction，含不存在的叶子）
                    │ 3. follow_symlinks=false 时逐级拒绝 link 组件
                    ▼
               受保护路径 / .git 写入拒绝
                    ▼
               编辑：读后写哈希 + 写前复核 + 写后校验 + create-only
                    ▼
Shell：cwd 同一边界校验 → 命令分级 → 审批(fail-closed) → 沙箱
```

## 4. 验证方式

```bash
python -m pytest -q                    # 64 passed（42 基础 + 22 安全/完成门/事务回归）
jinlin-agent eval --suite all          # 18/18 probes passed
```

评审建议的回归用例（SEC-001..008、EDIT-001..004、VERIFY-001..007、EVENT-001..004）
已覆盖大部分；**仍未覆盖**：Junction 在 L1 沙箱内的行为、多文件崩溃注入（EDIT-004，
需要 transaction manifest + 启动恢复）、嵌套/自由文本脱敏（SECRET-001/002）。

CI（`.github/workflows/ci.yml`，Windows + Ubuntu、Python 3.13）在本轮整改提交上已双平台通过。
CI 还暴露了一个真实缺陷并已修复：**同一秒内编辑 `.py` 文件时，CPython 的 mtime(秒级)+size
校验会让旧 `.pyc` 继续生效**，导致 L3 仍跑旧代码。修复：`atomic_write` 成功后按源文件粒度
清理 `__pycache__/<stem>.*.pyc`，并加测试 `test_edit_invalidates_stale_bytecode`。

## 5. 对评审评分的说明

- 同意"实验型工程 Harness 7.0 / 生产级 4.5"的判断：产品能力面、跨平台隔离、独立真实任务评测仍是短板。
- 本次整改的目标不是提高自评分，而是把"设计上强调可靠"落成"实现上强制可靠"：路径、cwd、编辑一致性、L0/L5/证据、事件完整性、并发回滚现在都有机器判定与回归测试。
- 通用 Claim-Evidence 语义匹配、多文件崩溃事务、最终 Token 预算闭环、递归脱敏仍待办，报告里不宣称已完成。
- 下一阶段优先级（与评审一致）：**独立真实任务评测 > 剩余 P2 > 扩展能力（MCP/Skills/UI/多 Agent）**。
