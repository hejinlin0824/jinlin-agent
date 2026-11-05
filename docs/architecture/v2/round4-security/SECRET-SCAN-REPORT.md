# Secret Scan Report（WP-R4-04，2026-11-05）

## 工具与命令

| 项 | 值 |
|---|---|
| 工具 / 版本 | **gitleaks 8.28.0**（官方 windows_x64 发行版，SHA 记录于执行机临时目录；未使用旧 CLI 语法） |
| 工作树扫描 | `gitleaks dir . --redact --report-format json --report-path reports/gitleaks-working-tree.json --no-banner` |
| 历史扫描 | `gitleaks git . --redact --report-format json --report-path reports/gitleaks-git-history.json --no-banner` |
| 退出码 | 两者均为 **1**（存在 findings；每条已分类，见下） |
| 扫描范围 | 当前工作树全部文件（含仓库根、`.github/`、配置、文档、fixtures、benchmark 派生产物与事件流）；完整 Git 历史（非浅层 clone，`git rev-list --all --count` = 372 个提交，全部 refs） |
| 脱敏 | 两份报告均以 `--redact` 生成后原样入库：`gitleaks-working-tree.json`（27,774 字节，41 findings）、`gitleaks-git-history.json`（133,509 字节，70 findings）。**报告不含任何命中原文** |

## 命中分类（逐条规则 ID / 路径 / 行号 / 字段名 / 判定）

全部命中（工作树 41 + 历史 70，其中 41 条为工作树的同一文件在历史中的重复计入；另有 29 条
来自历史提交 `a729547e` 中现已删除的仓库根 `home/sessions/sess_20261031_*` 旧版事件流）
为同一构成——gitleaks 规则 `generic-api-key` 把以下三个 **harness 生成的 opaque 标识符字段**
误判为 API key：

| 规则 ID | 文件 | 判定为标识符的字段（仅字段名，不含值） | 判定 | 处置 |
|---|---|---|---|---|
| generic-api-key | `docs/architecture/v2/benchmark/real/home/sessions/*/events.jsonl`（7 个会话流，41 处） | `session_id`、`payload.tool_call_id`、`payload.idempotency_key` | **误报**：harness 运行时生成的 uuid/hmac 型标识符，不是任何平台的凭证；同文件的 `model.attempt.usage`（真实用量）、`response_id`（模型响应元数据）未被标记 | 留档，不改事件流（原始事件流是审计证据） |
| generic-api-key | 同上文件在历史提交 `7ec9c69f` 等中的版本（41 处，与工作树重复计入） | 同上 | 误报（同上） | 留档 |
| generic-api-key | `home/sessions/sess_20261031_*/events.jsonl`（历史提交 `a729547e`，现已不存在于工作树；29 处） | `session_id`、`payload.idempotency_key` | 误报（同上；v1 时代审计事件流） | 留档；该目录已不存在，无需处置 |

分类方法：对每条命中定位其行，解析 JSON 后**只记录**匹配 20+ 位字母数字模式的字段名
（脚本化，值不落盘）。命中值全部来自事件信封/工具调用的标识符字段。

## 结论与决定

- **零真实凭证**。所有者提供的评测 key 仅经本轮 shell 环境变量注入，工作树与全部 372 个提交的
  历史中均无该值的任何形态（`ark-` 前缀 0 命中；上轮报告中的 "benchmark-integrity" 分支名
  字符巧合在本轮通用规则下不再构成命中）。
- **未添加任何 allowlist**：按任务书"最窄可解释"原则评估后，event-stream 路径级 allowlist 会
  掩盖该路径下未来的真实泄漏，弊大于利。findings 保持可见（退出码 1），以本报告的分类为准。
- 若未来 gitleaks 规则升级再次命中这些标识符字段，按同样方法重新分类即可。
