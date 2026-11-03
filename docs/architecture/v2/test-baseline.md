# 测试基线报告（WP-V2.0-0 交付物）

- 执行日期：2026-11-03
- 环境：Windows 11（win32 10.0.26200 x64）、Git Bash、Python 3.13（AppData\Local\Programs\Python\Python313）、零第三方运行时依赖
- 代码基线：`review/followup-fixes` @ `b0f3ade`（代码 HEAD `17b2262`，其后为文档提交）

## 结果

| 命令 | 结果 | 佐证 |
|---|---|---|
| `python -m pytest -q --junitxml` | **105 tests / 0 failures / 0 errors / 0 skipped**，退出码 0 | junit XML 属性 `tests="105" failures="0" errors="0" skipped="0"` |
| `python -m jinlin_agent eval --suite all` | **18/18 probes passed**，退出码 0 | eval 输出（HCS P1–P6/P3b + E1–E8 全 PASS） |
| CI（最近核实的第 10 轮整改 run） | Windows + Ubuntu 双平台绿 | run 41306414718（AGENT.md frontmatter 记录） |

测试构成（`wc -l`）：`tests/test_security.py` 1405（安全/完成门/编辑事务回归）、`test_sandbox_win.py` 405（Windows L1 沙箱实测，非 Windows 平台模块级跳过）、`test_trash.py` 368、`test_smoke.py` 163、`test_eval.py` 24（参数化 18 探针）。

## 观察项（不影响基线结论，供后续处理）

1. **Windows 控制台下 pytest 出现一次 reader-thread `UnicodeDecodeError` 警告**（`'utf-8' codec can't decode byte 0xb4`，pytest `PytestUnhandledThreadExceptionWarning`）：某子进程输出以 GBK/cp936 字节进入捕获管道。junit 显示 0 失败，属外观问题；提示仓库自身的 cp936 防护（PYTHONUTF8 等）未覆盖到**测试进程自身**的某条捕获路径。建议在后续 WP 顺手定位（疑与 sandbox/shell 测试相关）。
2. 与 AGENT.md 记录一致：`AGENT.md` frontmatter 记载 `pytest => 105 passed; eval => 18/18`——本机实测与记录一致，**无漂移**。
3. v2 迁移期的回归门槛建议即以本基线为准：任何 WP 合并前 105+ 全绿（只增不减）、18/18、CI 双平台绿。

## 复现步骤

```bash
cd "E:\codebase\玩耍项目\jinlin-agent"
git checkout review/followup-fixes   # b0f3ade 或其后
python -m pip install -e . && python -m pip install pytest
python -m pytest -q
python -m jinlin_agent eval --suite all
```
