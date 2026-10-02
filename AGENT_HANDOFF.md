# 微信归档任务交接

## 当前任务
- `TASK-20261001-173002-8057`，2026-10-02 已由 codex 领取；分支 `feat/archive-layer`，基线 `2abe708`。
- 用户要求完成整个任务；安全与人工验收门禁仍有效，不能将阶段 0 标记为整体完成。

## 环境实测
- 微信 4.1.15 已使用批准的兼容调试签名启动；CLI 实测 running (pid 64227, v4.1.15)。主号身份与历史消息仍待用户确认。
- `doctor`：DevToolsSecurity、_developer、lldb、python3 全通过；SIP enabled，保持不变。
- `status`：发现 3 个账号目录，均无 key、无 cache；尚不能判定哪一个是 howiefire。
- 专用构建实际文件名为 `target/release/wx-cli`，不是原计划写的 `target/release/wx`；没有覆盖旧 `~/.local/bin/wx`。
- 完整官方备份已验证；首轮重签启动失败后回滚，批准添加 disable-library-validation 的第二轮重签现已成功运行。当前应用仍是临时调试签名，取密钥后恢复官方备份。未取密钥、未解密、未读取真实聊天。

## 阶段 0：仓库密钥护栏
- 增加工作区与完整索引扫描、版本化 pre-commit hook、项目安全规则。
- Cargo.lock 的 checksum 与五处精确上游合成夹具行不是生产密钥；仅对上述精确内容豁免。
- SKILL.md 的手动密钥示例改为占位符，避免护栏把演示 hex 当真实密钥。
- 密钥护栏、SIP opt-in、密钥脱敏已按逻辑分别提交；本次提交整理既有归档原型与阶段报告。

## 阶段 0：SIP opt-in 整理
- 保留既有 `--allow-sip-enabled` 实现，默认仍拒绝 SIP enabled；opt-in 只跳过 SIP，不跳过其他预检。
- `cargo test -p wx-keychain`：62 passed；`cargo clippy` 成功。
- release CLI 的 `key extract --help` 已实际显示 opt-in 参数；未执行取密钥。


## 阶段 0：密钥脱敏整理
- 保留既有修改：`key extract` 不再打印原始 key；LLDB 日志只保存调用计数与完成状态，权限设为 600。
- 以上路径已编译通过，真实 LLDB 捕获与日志权限仍需 G2 实测；不能把静态检查写成真实取密钥通过。

## 阶段 0：归档原型与证据
- 原型保持原有功能和测试，不宣称实现阶段 3/4 的完整接口。`pytest` 7 passed。
- 实际 CLI 烟测：同秒五条、重复导入、search/context 顺序均正确；stdio MCP initialize/tools/list/search 成功。全为合成数据，临时数据已自动删除。
- 密钥门禁烟测捕获工作区、索引独有、大小写 key、手动 key 命令及夹具文件新增 key；实际提交被 hook 拦截，输出不含匹配值。
- 阶段报告：`docs/reports/phase-0.md`；阶段 1 的恢复执行与回滚记录：`docs/reports/phase-1.md`（G1 未通过）。
- 当前缺口仍为计划原型缺口：local_id 兜底、增量/对账/发现、FTS/引用信息、白名单、定时、完整 MCP。必须按既定门禁继续，不能把当前原型交付为整个任务完成。

## 阶段 1：4.1.15 重签名启动失败，已回滚
- 完整备份：`~/Library/Application Support/howie-wechat-archive/setup-backup-20261002-200929/WeChat.app`；备份通过 deep/strict 官方签名验证。
- 从新版应用导出 XML entitlements，只添加 get-task-allow。首次导出未加 --xml 导致 plist 解析失败；显式 --xml 后成功。
- 临时重签主应用，保留嵌套组件原签名和 hardened runtime；签名/entitlements 静态验证通过，但实际启动失败。
- 原始诊断：`~/Library/Logs/DiagnosticReports/WeChat-2026-10-02-201142.ips`。DYLD 拒绝加载 libwxld.dylib，因为 ad-hoc 主应用与腾讯签名 dylib 的 Team ID 不同。
- 已用完整备份恢复并再次验证腾讯官方签名，官方应用实际启动成功；SIP 未关闭、没有 sudo 或取密钥。
- 用户已选择继续临时调试方案，批准仅对主应用添加 disable-library-validation；执行计划已更新，不批量重签嵌套组件。


## 阶段 1：批准方案已准备，退出微信被取消
- 当前官方应用的 CDHash 与已验证备份一致，签名 Team ID 仍为腾讯；重新导出的原始 entitlements 与备份一致。
- 批准的 plist 已写到备份目录的 `debug-entitlements-library-validation.plist`（600），只添加 get-task-allow 与 disable-library-validation。
- 正常退出命令 `osascript -e 'tell application "WeChat" to quit'` 返回 `User canceled. (-128)`。未强制结束进程，未再次重签，未取密钥。

## 阶段 1：兼容签名实际启动成功，等待历史确认
- 用户明确通知微信已退出后，未再次发送退出命令；直接实施已批准的第二轮签名。
- `codesign --force --sign - --options runtime --timestamp=none --entitlements <批准plist> /Applications/WeChat.app` 与 deep/strict 验证均退出 0。
- 签后 entitlements 与批准文件完全一致，所有原始值保持不变；flags 为 `0x10002(adhoc,runtime)`，两项临时调试权限为 true。
- `open /Applications/WeChat.app` 后，CLI 实测 running (pid 64227, v4.1.15)，三个账号目录均无 key/cache；doctor 除 SIP enabled 外全通过。只证明进程启动，不证明主号与历史消息可见。

## 下一道门禁
请用户在当前已启动的微信里确认主号 howiefire 与历史聊天（包括「哥飞的朋友们」）正常可见；明确确认后才进入取密钥阶段。当前是临时调试签名，取密钥后恢复完整官方备份；若用户暂停或 UI 异常，先恢复官方应用。`key list` 会打印密钥，不能直接运行。
