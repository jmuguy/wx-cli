# 微信归档任务交接

## 当前任务
- `TASK-20261001-173002-8057`，2026-10-02 已由 codex 领取；分支 `feat/archive-layer`，基线 `2abe708`。
- 用户要求完成整个任务；安全与人工验收门禁仍有效，不能将阶段 0 标记为整体完成。

## 环境实测
- 用户已升级至 4.1.15；官方应用恢复后 CLI 实测 running (pid 37288, v4.1.15)。主号身份与历史消息仍待用户确认。
- `doctor`：DevToolsSecurity、_developer、lldb、python3 全通过；SIP enabled，保持不变。
- `status`：发现 3 个账号目录，均无 key、无 cache；尚不能判定哪一个是 howiefire。
- 专用构建实际文件名为 `target/release/wx-cli`，不是原计划写的 `target/release/wx`；没有覆盖旧 `~/.local/bin/wx`。
- 已备份并临时重签名，启动遇到 DYLD Team ID 校验失败，已恢复官方应用并验证正常运行。未取密钥、未解密、未读取真实聊天。

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
- 下一方案是临时对主应用添加 disable-library-validation，或暂缓取密钥；这超出原计划的“只加 get-task-allow”，需用户选择后更新方案。未继续试其他签名、未批量重签嵌套组件。


## 下一道门禁
先让用户确认已恢复的官方微信与历史消息正常。对 DYLD Team ID 错误的修订方案需用户选择；批准后才再次备份/重签并要求用户确认主号与历史消息可见，再取密钥。`key list` 会打印密钥，不能直接运行。
