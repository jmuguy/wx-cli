# 阶段 1：非破坏性环境核查（未完成）

日期：2026-10-02。仅执行版本核对、release 构建、doctor/status/help；没有开始备份、重签名或取密钥。G1 未通过。

## 已执行命令与原始输出摘录

`/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' /Applications/WeChat.app/Contents/Info.plist`：

```text
4.1.5
```

`id -Gn` 输出含 `_developer`，不需补组。`cargo build --release`：

```text
Compiling wx-cli v0.7.4
Finished `release` profile [optimized] target(s) in 9.51s
```

按原计划执行 `target/release/wx doctor/status/key extract --help` 均返回 command not found（127）。读取构建目录后确定实际二进制名是 `wx-cli`，已更正计划；没有建立兼容别名或覆盖旧安装。

`target/release/wx-cli doctor`：

```text
✗  SIP disabled             SIP is enabled
✅  DevToolsSecurity         DevToolsSecurity is enabled
✅  _developer group         user is in _developer group
✅  lldb                     lldb-2100.0.17.203
✅  python3                  Python 3.14.7
```

`target/release/wx-cli status`：

```text
WeChat:   not running
Accounts:
  jmuguy_6a0d  key ✗  no cache
  wxid_e1xwpk9mdgvf22_1110  key ✗  no cache
  wxid_7jh0z93utey012_176d  key ✗  no cache
Paths:    config ~/Library/Application Support/wx-cli/config  cache ~/Library/Caches/wx-cli
```

`target/release/wx-cli key extract --help` 显示：

```text
Usage: wx-cli key extract [OPTIONS]
--timeout <TIMEOUT>  Timeout in seconds for LLDB capture [default: 120]
--allow-sip-enabled  Attempt LLDB extraction with SIP enabled (requires a debuggable WeChat signature)
```

注意：help 的摘要仍是上游默认 SIP-disabled 路线，不表示应关闭 SIP。此次不执行提取。

## 缺失前提与恢复执行步骤

1. 用户通过官方渠道将微信升级到至少 4.1.7，并登录主号 howiefire；AI 不自动安装或替换微信应用。
2. 用户通知完成后，由 AI 重新核对版本、运行状态与主号身份，不根据目录名猜账号。
3. 按既有计划完整备份升级后的 WeChat.app、重新导出 entitlements；不使用旧 4.1.5 的 plist。
4. 重签名后需用户确认历史消息可见，再取密钥。若历史不可见立即恢复原应用。
5. G2 解密通过后才能验证真实 export 的 ID/空窗口行为并进入阶段 3；仍需独立阶段验收。

未做 App 修改、sudo、安全设置修改、微信启动/重启、密钥读取、真实聊天导出或 MCP 注册。任务池应记录 blocked，而不是 done 或整个任务 review。
