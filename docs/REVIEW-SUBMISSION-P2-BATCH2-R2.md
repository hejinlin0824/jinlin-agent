# P2 安全加固第 2 批 · 第 9 轮整改报告（供独立审查）

| 项 | 值 |
|---|---|
| 基线 `main` | `249bbab`（**未改动**，等复核批准后 ff） |
| 分支 | `review/followup-fixes` |
| 上一轮代码 HEAD | `a6c75a0`（第 8 轮整改末） |
| **本轮代码 HEAD** | 见 §1 提交列表（`ea398ee` 之后为文档提交，单独标注） |
| 对应复核 | 《第二阶段 P2 安全加固第 2 批：独立复核意见》（P0×2 阻断 + P1 替换竞态阻断 + CI 证据要求） |
| 本机验证 | pytest **100 passed**、eval **18/18**（CI 数字见 §7） |

## 1. 结论摘要（对照复核 §八/§九）

| 复核项 | 上轮结论 | 本轮处理 | 测试 |
|---|---|---|---|
| P0：move→open 之间替换窗口 | 阻断 | 生命周期改为 **move-first**：`os.replace` 成功后**立即登记 pending 记录**（backup 路径、expected sha、原始字节、verified=False），验证通过 one 个新开的 fd 重新哈希 + fstat；哈希或指纹不一致 → backup **永不恢复**、原始字节按记录重建到路径（路径被占则入 rescue 文件）→ `RECOVERY_UNCERTAIN` | `test_delete_backup_swap_between_move_and_open_is_not_restored`（os.open 注入，复核 §2.3 指定场景；旧实现把替换文件当原文恢复 → 红） |
| P0：哈希不匹配 ≠ "原文在 backup" | 阻断 | 取消"mismatch → 恢复 backup"路径；恢复内容**只来自事务记录里的已验证字节**（不来自 backup 路径），backup 中无论是什么都保持原样并报告 | 同上（断言路径 = 原始字节、backup = 替换内容、两者都不丢） |
| P0：移动成功后异常、事务未登记 | 阻断 | pending 记录在 `os.replace` 之后、任何可失败步骤之前写入 `applied`；open/read/fstat 任一失败的注入测试均断言"路径完整 + backup 被回滚记录在案" | `test_delete_backup_verification_open_failure_recovers_original`、`..._verification_read_failure_recovers_original`、`..._verification_fstat_failure_recovers_original` |
| P1：clean 删除前未核验文件对象 | 阻断 | `record_trash` 创建时捕获四元组指纹入清单；`scan_trash` 与 `clean --apply` 在 unlink 前对**当前 stat** 与清单指纹比对，不匹配 → report-only/failed（附原因），不删除 | `test_clean_apply_does_not_delete_replaced_file_after_scan`（扫描后替换注入；旧实现按路径删除 → 红） |
| P1：forget 失败无诊断 | 已补 | 提交清理成功 unlink 后 `forget_trash` 返回 False → 计入 `trash_manifest_degraded` 诊断 | `test_commit_cleanup_forget_failure_is_reported` |
| P1：manifest 原子重写的断电持久性 | 文档边界 | 保持临时文件+fsync+replace；**目录项 fsync 未做**，已在 trash.py 文档与手册 §16 明确"原子替换 ≠ 全部断电场景持久"，不扩大声明 | （文档边界，无代码） |
| P2 legacy / P3 四字段碰撞 | 上轮已通过 | 无改动（P3 的 Linux/Windows 差异继续保留在 §残余风险） | — |
| CI 证据 | 待补 | 本报告 §7 给出真实 Run ID + 双平台 Job 链接 + 最终代码 HEAD | — |

**诚实记录：本轮发现并修复了一个我自己引入的严重缺陷** —— 为保留 `RECOVERY_UNCERTAIN` 语义而改写的异常处理器存在**控制流跌落**：当异常是非 AgentError（如 PermissionError）且回滚干净时，处理器两个分支都不命中，控制流落到 try/except 之后，**把已回滚的事务当成已提交**（ok=True）。三条验证失败注入测试正好钉住了它。修复后处理器每个分支必然 return（AgentError 按自身 code 上报；其他异常包 TOOL_EXECUTION_FAILED）。

## 2. 新生命周期（P0）

```text
plan 校验（读字节 + sha，存 original_bytes）
→ os.replace(path, backup)                    # 移动成为事实
→ applied.append(pending delete 记录)          # ← 立即登记：backup、expected sha、原始字节、verified=False
→ fd = os.open(backup) → 哈希 + fstat(同一对象) # 验证内容与指纹
→ 一致：verified=True、record_trash、提交后按指纹守卫清理
→ 不一致/损坏：raise RECOVERY_UNCERTAIN         # backup 永不恢复；
                                               # 回滚按记录的原始字节重建路径
```

回滚侧（delete）：`backup_verified=False` 或 哈希/指纹不再匹配 → **不做任何恢复动作**，从记录的原始字节在路径上重建（O_EXCL，被占则保留现场报 UNCERTAIN），未证明的 backup 原样留存。

## 3. 新增/重写测试（8 条，全部先红后绿）

| 测试 | 覆盖时序 |
|---|---|
| `test_delete_backup_swap_between_move_and_open_is_not_restored` | 复核 §2.3 指定：替换落在移动后、验证 open 前 → 检出 mismatch → 原文重建、替换留存、UNCERTAIN（旧实现把替换当原文恢复 → 红） |
| `test_delete_backup_verification_open_failure_recovers_original` | 验证 open 失败（移动后）→ pending 记录使回滚按字节重建路径（旧实现路径丢失 → 红） |
| `test_delete_backup_verification_read_failure_recovers_original` | 验证读失败（fd 定向注入，不影响 pytest 自身捕获） |
| `test_delete_backup_verification_fstat_failure_recovers_original` | 验证 fstat 失败 |
| `test_clean_apply_does_not_delete_replaced_file_after_scan` | 复核 §三 指定：scan 后、unlink 前同路径替换 → 替换存活 + 报告含 ownership 不匹配（旧实现按路径删除 → 红） |
| `test_commit_cleanup_forget_failure_is_reported` | 清理成功但清单更新失败 → 事务成功 + 降级诊断可观测（旧实现静默 → 红） |
| `test_delete_backup_swap_between_move_and_check...` 重写 | 清理窗口的指纹守卫（stat #2 注入） |
| round-4 `..._delete_conflict_preserves_external_file` 断言细化 | 新契约：原文按字节恢复到路径 + 外部内容留存于备份，两者都不销毁 |

## 4. 残余竞态（如实声明）

1. **stat→unlink 微秒窗口**（创建清理/提交清理/clean）：指纹比对通过后、unlink 前的替换仍会按"自己的文件"删除——指纹碰撞测试已钉死该边界；隔离区移动方案（复核 §三 建议）作为后续硬化项评估。
2. **恢复字节的可信度**：回滚重建依赖 `original_bytes`（哈希验证过的读取内容）——若外部进程在验证前的窗口内改写了内容且哈希恰好等于 expected（需要刻意碰撞），重建的即为其内容；威胁模型外。
3. **manifest 原子替换的目录项持久性**：fsync 未覆盖父目录，断电场景的目录项变更不保证——已如实声明，不宣称全场景持久。
4. **manifest 可被有写权限的外部进程篡改**：无权限提升（该进程本可写 workspace），清单损坏按无证明处理。

## 5. 本机结果

```
python -m pytest -q          -> 100 passed
jinlin-agent eval --suite all -> 18/18 probes passed
```

## 6. CI

最终 HEAD 的 Run ID 与双平台 Job 链接：推送后由文档提交补记（见分支 Actions 页与本报告更新）；判定标准：两 Job 均 success。过程记录：本轮中途一次 Ubuntu 失败（run 41297593358，见上一轮报告 §8a）暴露了异常处理器控制流跌落缺陷（§1 诚实记录），修复后转绿。

## 7. 关闭与排期

| 已关闭 | 仍排期 |
|---|---|
| 第 4 轮 P1×2（已合并）；第 6/8/9 轮全部 P0/P1/P2 项；clean 身份核验；事务登记时序 | 隔离区移动硬化（复核建议，评估中）；`clean` 并入会话自动清扫；Python 3.11/3.12 矩阵；30–50 真实任务评测；覆盖率基线 |

## 8. 审查清单

- [ ] `pytest -q` = 100 passed（Ubuntu 平台守卫 skip 除外）；eval 18/18
- [ ] pending 记录先于验证写入；验证 open/read/fstat 失败三条测试均断言路径完整 + backup 在案
- [ ] mismatch 永不恢复 backup；原始字节重建 + 替换留存 + UNCERTAIN
- [ ] `clean --apply` 删除前比对当前 stat 与清单指纹；替换文件存活 + 报告含原因
- [ ] 处理器无控制流跌落（碰撞测试的 Linux 分支可证）
- [ ] 残余边界声明（§4）与代码一致
- [ ] `main` 仍为 `249bbab`
