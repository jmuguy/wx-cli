# 阶段 0：整理现有工作

日期：2026-10-02。任务：`TASK-20261001-173002-8057`。基线 `2abe708`；分支 `feat/archive-layer`。结论：G0 技术门禁通过；独立 Claude 验收未执行。整个任务未完成。

## 仓库整理

执行 `git switch -c feat/archive-layer`，输出：

```text
Switched to a new branch 'feat/archive-layer'
```

既有改动按 SIP opt-in、密钥脱敏、归档原型拆分；另增加护栏提交。未 push，未覆盖旧 CLI，未动微信签名/密钥/数据库。前置提交：

```text
24f2c73 feat: guard local archive commits against plaintext keys
5057ff7 feat: allow explicit SIP-enabled LLDB preflight
3f633e2 fix: keep captured database keys out of terminal and logs
```

原型及本报告在最终阶段 0 提交中；各逻辑提交同次更新 AGENT_HANDOFF.md。

## G0 命令与输出

`cargo test -p wx-keychain`：

```text
running 62 tests
test sip_override_tests::sip_override_keeps_other_checks_and_default_requirement ... ok
test result: ok. 62 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
Doc-tests wx_keychain: 0 passed; 0 failed
```

`cargo clippy`：

```text
Checking wx-db v0.7.4
Checking wx-context v0.7.4
Checking wx-monitor v0.7.4
Checking wx-media v0.7.4
Finished `dev` profile [unoptimized + debuginfo] target(s) in 22.10s
```

`python3 -m pytest -q archive/tests`：

```text
.......                                                                  [100%]
7 passed in 0.11s
```

`scripts/check-no-secrets.sh`（提交时 pre-commit 同样运行）：

```text
PASS: working tree and index contain no unapproved key patterns
```

扫描覆盖非忽略工作区文件（含未跟踪 docs）与完整 Git 索引；不输出匹配值。精确豁免 Cargo.lock checksum 行以及五处上游合成夹具行，不豁免整个文件。最初扫描发现 SKILL.md 的手动 key 演示串，已改成占位符；仅工作区修复时索引仍被拦截，更新索引后通过。

## 护栏运行时验收

在自动清理的临时 Git 仓库中，使用随机生成的合成 key，逐次运行 `scripts/check-no-secrets.sh <临时仓库>`；退出码与输出都断言，且断言随机 key 未出现在输出。实际安装版本化 hook 后执行 `git commit -m 'test: must be blocked'`，返回非零。

```text
clean worktree: passed
untracked lowercase key: blocked
untracked uppercase key: blocked
manual key command: blocked
staged key with clean working copy: blocked
clean working copy and index: passed
checksum file containing additional secret: blocked
Cargo checksum-only exception: passed
exact upstream fixture exception: passed
new secret in fixture file: blocked
actual pre-commit attempt: blocked, no key emitted
```

本仓已执行 `git config --local core.hooksPath scripts/git-hooks`。没有覆盖任何既有 pre-commit hook（原来只有 sample 文件）。

## 原型 CLI 与 MCP 烟测

临时目录创建五条同秒消息的合成 export，账号 synthetic、会话 synthetic@chatroom；运行真实 Python 程序而非替代实现：

```text
python3 archive/wechat_archive.py --root <临时数据根> import <合成export> --account synthetic
# 再导入一次
python3 archive/wechat_archive.py --root <临时数据根> search 合成 --account synthetic
python3 archive/wechat_archive.py --root <临时数据根> context --account synthetic --talker synthetic@chatroom --server-id 103 --radius 2
python3 archive/wechat_archive.py --root <临时数据根> status
python3 archive/wechat_archive.py --root <临时数据根> mcp
```

MCP stdin 依次送 initialize、notifications/initialized、tools/list、tools/call(search) JSON-RPC 行；解析 stdout 并断言消息身份。观测摘录：

```json
{"data":"synthetic_only","cli_import":5,"repeat_import_total":5,"search_server_ids":[105,104,103,102,101],"context_server_ids":[101,102,103,104,105],"mcp_protocol":"2024-11-05","mcp_tools":["search","get_context"],"mcp_search_server_ids":[105,104,103,102,101],"directory_mode":"0o700","database_mode":"0o600"}
```

临时夹具、库、原文与护栏试验目录均由 TemporaryDirectory 清理。

## 未验证项与边界

- 当前 MCP 仅是既有原型接口；未满足阶段 4 的完整引用字段、list_conversations 和白名单。
- 未完成 local_id 兜底、增量调度、历史对账、FTS、会话白名单、launchd；未作阶段 3 变异验收。
- 未升级/重签名/重启微信，未取密钥或解密，未做 G2 日志检查。
- 未读取真实群聊，未核对 UI 消息数/发送人/附件，未完成三个真实问答场景。
- 未注册用户级 MCP，未在 Claude Code 中调用工具。
- G0 通过不代表 G1–G5 通过；继续执行需遵守计划中人工安全门禁。
