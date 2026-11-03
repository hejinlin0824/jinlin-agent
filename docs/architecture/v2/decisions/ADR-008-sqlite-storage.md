# ADR-008：存储 = 事件 JSONL（执行真相）+ stdlib SQLite（任务 Ledger）；不引入消息中间件

- 状态：Accepted（2026-11-03 负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §11.2/§18-8；docs/PLAN.md D5；AGENTS.md 硬规则 1

## 背景

docs/PLAN.md D5 早已裁定："事件日志（JSONL，append-only）是唯一真相；SQLite 仅作可重建投影（留接口）"。v2 §11.2 建议 SQLite 起步做 Task Ledger，与本仓库既有决策兼容；v2 §11.3 列出的禁入项（Kafka/分布式调度/消息总线/向量库）与 AGENTS.md 硬规则 1（零第三方运行时依赖）完全一致。

## 决策

1. 会话事件日志：维持 `~/.jinlin-agent/sessions/<sid>/events.jsonl`，语义不变。
2. Task Ledger：stdlib `sqlite3`（Python 自带，不破坏零依赖），单文件（默认 `~/.jinlin-agent/ledger.db`，路径入 config 且项目级禁改，模式沿用 `storage.home` 的规则），WAL 模式 + `user_version` 做 schema 迁移。
3. Ledger 全部写操作同步发事件（→ ADR-003 的可重建性）；Ledger 损坏时可从全量事件重建——提供 `jinlin-agent ledger rebuild` 子命令并配测试。
4. 第一阶段并发模型：进程内多 Worker（线程池）+ 单进程 Ledger 写者（SQLite 连接检查 `check_same_thread` 策略在 WP-1 定）；跨进程并发（多个 CLI 同时跑）只依赖会话锁与工作区写锁的既有语义，Ledger 层加文件锁防并发建库。
5. 不引入：消息中间件、Redis、独立数据库服务、向量库（v2 §11.3；经验沉淀第一阶段用项目级结构化记录，v2 §5.5）。

## 备选方案

- Ledger 也用 JSONL（与事件同构）：可行备选——若 SQLite 在 Windows 上的锁行为出问题；接口（Repository）按可替换设计，切换不动调用方。
- Pydantic/SQLAlchemy 等 ORM：否决——硬规则 1。

## 后果

- `pytest` 全绿约束延伸：Ledger 的迁移与重建必须有单测（v2 §14-WP1 交付物：状态机测试、重启恢复测试）。
- 对破坏性 schema 变更提供回滚方案（v2 §20.2：`user_version` 降级脚本或重建路径）。
