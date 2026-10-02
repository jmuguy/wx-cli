# 阶段 1：环境核查与重签名回滚（未完成）

日期：2026-10-02。首次执行为非破坏性环境核查；用户升级到 4.1.15 后已备份、临时重签名并尝试启动，因 DYLD Team ID 校验失败立即回滚。官方应用已恢复并实际启动成功。G1 仍未通过，未取密钥。

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

1. 用户已完成官方微信升级到 4.1.15；主号 howiefire 身份与历史消息仍需确认。
2. 用户通知完成后，由 AI 重新核对版本、运行状态与主号身份，不根据目录名猜账号。
3. 按既有计划完整备份升级后的 WeChat.app、重新导出 entitlements；不使用旧 4.1.5 的 plist。
4. 重签名后需用户确认历史消息可见，再取密钥。若历史不可见立即恢复原应用。
5. G2 解密通过后才能验证真实 export 的 ID/空窗口行为并进入阶段 3；仍需独立阶段验收。

首次环境核查未做 App 修改；恢复执行的实际操作与回滚证据如下。未执行 sudo、安全设置修改、密钥读取、真实聊天导出或 MCP 注册。任务等待修订签名方案选择，不能标记 done。

## 4.1.15 恢复执行：签名校验通过但启动失败

用户报告已升级到 4.1.15。`codesign --verify --deep --strict /Applications/WeChat.app` 验证原始官方签名成功；原签名 Authority 为 Tencent Mobile International Limited，TeamIdentifier 为 `5A4RE8SF68`。应用约 1.4G，磁盘可用约 40Gi。

完整备份命令：

```text
ditto /Applications/WeChat.app '~/Library/Application Support/howie-wechat-archive/setup-backup-20261002-200929/WeChat.app'
codesign --verify --deep --strict <备份>/WeChat.app
```

上述路径的 `~` 在实际执行中使用 `/Users/jmuguy` 绝对路径；命令均退出 0。签名元数据和 entitlements 保存在同一仓外私有目录，文件权限 600。

`codesign -d --entitlements -` 的默认输出不能被 plistlib 解析，首次解析报 InvalidFileException。改用 `codesign -d --entitlements - --xml /Applications/WeChat.app` 后成功；不复用旧版本的 entitlements。原始 get-task-allow 为 false，调试文件仅添加 get-task-allow=true，其他原始值完全保留。

临时签名命令：

```text
codesign --force --sign - --options runtime --timestamp=none --entitlements <新版debug-entitlements.plist> /Applications/WeChat.app
codesign --verify --deep --strict /Applications/WeChat.app
open /Applications/WeChat.app
```

签名与静态校验均退出 0，实际 flags 为 `0x10002(adhoc,runtime)`，签后 entitlements 与准备文件完全一致。没有使用 --deep 签名，嵌套组件保留腾讯原签名。

实际 `wx-cli status` 显示 not running；用系统进程表独立核对，无 WeChat.app 进程。诊断文件 `~/Library/Logs/DiagnosticReports/WeChat-2026-10-02-201142.ips` 的决定性原始证据：

```text
app_version: 4.1.15
termination.namespace: DYLD
termination.indicator: Library missing
Library not loaded: @rpath/libwxld.dylib
mapping process and mapped file (non-platform) have different Team IDs
```

结论：不是微信版本不满足要求，也不是 dylib 文件不存在；运行时 library validation 拒绝 ad-hoc 主程序加载原腾讯签名的 dylib。上游 issue #20 仅报告 4.1.13 的成功案例，没有给出完整重签命令，不能作为本机 4.1.15 已验证证据。来源：<https://github.com/pandorafuture/wx-cli/issues/20>。

## 回滚与运行证明

立即从完整备份恢复：

```text
ditto <备份>/WeChat.app /Applications/WeChat.app
codesign --verify --deep --strict /Applications/WeChat.app
open /Applications/WeChat.app
target/release/wx-cli status
```

恢复与签名校验退出 0，签名恢复为官方 Developer ID，TeamIdentifier 为 `5A4RE8SF68`。CLI 实际输出：

```text
WeChat:   running (pid 37288, v4.1.15)
Accounts:
  jmuguy_6a0d  key ✗  no cache
  wxid_e1xwpk9mdgvf22_1110  key ✗  no cache
  wxid_7jh0z93utey012_176d  key ✗  no cache
```

重签阶段的 doctor：除 SIP enabled 外，DevToolsSecurity、_developer group、lldb、python3 全通过。SIP 保持 enabled。

## 当前边界与待决策

官方应用已恢复，未取密钥或改动聊天数据；用户尚未核对 UI 历史。下一候选是临时给主应用添加 disable-library-validation，让腾讯签名 dylib 可被 ad-hoc 主程序加载；这会临时降低该应用的库加载校验，超出原计划的“仅添加 get-task-allow”，需用户选择后更新方案再实施。另一选择是暂缓取密钥，保持官方应用。

未尝试关闭 SIP、批量重签嵌套组件、去除 sandbox 或继续未知签名方案。
