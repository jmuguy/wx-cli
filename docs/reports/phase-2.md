# 阶段 2：取密钥、解密与官方应用恢复

日期：2026-10-02。G2 技术检查完成；独立 Claude 验收尚未获得结论。只处理用户已确认的主号 howiefire；未关闭 SIP，未继续保留调试应用签名。

## 捕获前提与失败原因

用户已确认主号与「哥飞的朋友们」历史正常（见阶段 1）。完整官方 4.1.15 备份：

`~/Library/Application Support/howie-wechat-archive/setup-backup-20261002-200929/WeChat.app`

第一次 `target/release/wx-cli key extract --allow-sip-enabled --timeout 180` 在 attach-wait 路线超时：180 秒内 PBKDF2 回调数为 0。LLDB 受控 attach/detach 成功，符号存在；受控 launch 的入口断点确认真实派生调用存在，轮数为 256000。没有将超时当作成功，也没有放宽 SIP 或密钥验证。

修复 `crates/wx-keychain/src/lldb.rs`：显式指定应用主程序，由 LLDB `process launch --stop-at-entry` 启动，在继续执行前安装捕获脚本；管理进程保持 stdin 管道打开，避免 attach 时序错过启动派生。重编译后运行同一捕获命令成功。

成功输出中的非秘密摘录与观测：

```text
Key captured after 73 PBKDF2 calls.
Matched account: wxid_e1xwpk9mdgvf22_1110
```

私下解析 keystore，仅输出结构计数：账号数 1，与批准主号对应的账号目录一致；26 个 per-DB 派生密钥对。报告不包含任何密钥值。终端/诊断流未出现实际密钥或 64-hex 密钥模式。

## 权限修复

上游旧写入方式产生过 0644 的 keystore，发现后立即收紧为 0600，父目录为 0700。永久修复 `crates/wx-keychain/src/store.rs`：新建父目录按 0700；以 `create_new`、Unix mode 0600 创建临时文件，写完后原子替换，不跟随或截断已有临时文件。增加新建及替换权限回归测试。

实际核查：

```text
keys.toml: 0600
config directory: 0700
saved_account_count: 1
per_database_key_pairs: 26
```

密钥保存在本机 `~/Library/Application Support/wx-cli/config/keys.toml`，不是 macOS 系统钥匙串。归档配置、SQLite、报告与提交不保存密钥。

## 解密和会话边界

`target/release/wx-cli decrypt --account wxid_e1xwpk9mdgvf22_1110`：

```text
Cache: 26 decrypted, 0 cached, 0 errors, 0 WAL patched
```

`target/release/wx-cli sessions --account wxid_e1xwpk9mdgvf22_1110 --all --format json --no-server`：1664 个会话，无 skipped，无后续分页。精确名称解析到批准群 `45416093717@chatroom`；另两个含「哥飞」的群未纳入归档白名单。

批准群本地源记录数 83211；最早可用消息为 Unix 秒 1699000614，即 `2023-11-03T16:36:54+08:00`。首次归档从这一实际起点覆盖全部本地历史，不以近期窗口替代全量范围。

## 完整官方应用恢复

捕获与首次解密成功后，恢复上述完整官方备份并重新启动。实际检查包括：

```text
codesign --verify --deep --strict /Applications/WeChat.app
codesign -dvvv /Applications/WeChat.app
codesign -d --entitlements - --xml /Applications/WeChat.app
target/release/wx-cli status
target/release/wx-cli decrypt --account wxid_e1xwpk9mdgvf22_1110 --incremental
```

观测：deep/strict 校验退出 0；官方 TeamIdentifier 为 `5A4RE8SF68`，entitlements 与完整官方备份相同，临时 get-task-allow/disable-library-validation 已移除。

```text
WeChat: running (pid 83205, v4.1.15)
Cache: 10 decrypted, 16 cached, 0 errors, 0 WAL patched
```

这是恢复后真实运行与解密复验，不只是静态签名。恢复后未重新做人工逐条聊天核对，该项属于 G5。

## 日志泄漏检查与代码门禁

在读取 keystore 的进程内比较全部已保存秘密值，不打印它们。扫描边界和结果：

| 位置 | 文件数 | 64-hex 模式 | 实际已存秘密匹配 |
|---|---:|---:|---:|
| `~/Library/Logs/wx-cli`（不存在） | 0 | 0 | 0 |
| `$TMPDIR/wx-cli` | 2 | 0 | 0 |

这不是“全盘没有任何密钥”的声明：授权 keystore 本来必须保存密钥。应用备份、归档配置、日志和数据均留在本机私有目录。

父会话执行：

```sh
cargo fmt --all && cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings && cargo build --release -p wx-cli
```

退出 0。cargo 输出统计：798 passed、11 ignored，46 个测试套件；clippy 无警告；release 构建通过。此统计为多套件输出汇总，不是伪造的一行 cargo 原文。

## 未验证项

- 独立 Claude 验收：调用真实 Claude CLI 返回 API 429，`You've hit your session limit · resets 11:40pm (Asia/Manila)`，输入/输出 token 均为 0，没有审查结论。
- 全量归档与历史缺失媒体处理见阶段 3，不在此宣称完整归档成功。
- 微信 UI 计数、同秒组和三个真实问题仍须阶段 5 人工验收。
