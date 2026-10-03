# wx-archive 交接

更新：2026-10-03。任务 TASK-20261001-173002-8057；分支 feat/archive-layer，不推 main。完整决策见 docs/PLAN-wechat-archive.md，证据见 docs/reports/phase-0..5.md。此文件取代“等待 G1 历史确认、尚未提取密钥”的旧交接；历史失败/退出取消证据仍在 phase-1。

## 当前可运行状态

- 用户确认主号 howiefire/水龙会 Howie 历史与哥飞的朋友们可见；实际账号 wxid_e1xwpk9mdgvf22_1110，批准群仅 45416093717@chatroom。
- WeChat 4.1.15；临时调试已结束。完整官方应用恢复，Tencent TeamIdentifier 5A4RE8SF68、原 entitlements 一致、deep/strict 签名通过并实际启动；不再有 get-task-allow/disable-library-validation。不要重新签名或关 SIP。
- 受控 stop-at-entry 启动捕获 73 次 PBKDF2 调用，匹配本账号 26 数据库。首次 decrypt 26 成功/0 错误；官方恢复后增量 10 解密/16 缓存/0 错误。密钥及盐只在仓外私有 TOML，文件 0600/目录 0700；不是 macOS 系统 Keychain，不输出值。
- 本地完整归档已实跑；截至观察 checkpoint 1790993955（2026-10-03 10:19:15 +08），源与归档均 83214 条、skipped 0。最早 2023-11-03 16:36:54 +08。后续 overlap 再跑新增/修订均 0。
- 可用媒体消息 4522、明确本地缺失 6091，metadata_only 0；4512 去重文件/1368953068 字节，内容与寻址目录逐项一致、模式 0600。不联网补下载，不声称历史媒体字节完整；四类型 manifest 及富卡片边界见 phase-3。
- 主归档 schema v2：完整 export 校验、原始 JSON/附件留存、修订、中文 FTS、稳定 server/local 身份、重叠增量、完整近 30 天对账、白名单发现、读写锁。未知错误始终硬失败。曾遇 EINTR 硬停，已修复完整目录重启和每 export 一次图片索引后全历史成功。

## 私有位置与运行命令

私有 ROOT：`/Users/jmuguy/Library/Application Support/howie-wechat-archive`，配置 config.toml；数据不进 git。配置绑定主号/唯一群，initial_since=1699000614、chunk=604800、overlap=3600、settle=120、reconcile=30、no_media=false。应用/客户端配置备份在 ROOT 的 setup-backup-20261002-200929，保留，不清理。

```bash
cargo build --release -p wx-cli
/opt/homebrew/bin/python3 archive/wechat_archive.py --root "$ROOT" sync-incremental --talker 45416093717@chatroom
/opt/homebrew/bin/python3 archive/wechat_archive.py --root "$ROOT" reconcile --talker 45416093717@chatroom --days 30
/opt/homebrew/bin/python3 archive/wechat_archive.py --root "$ROOT" mcp
```

自动采集先刷新本地解密缓存，实际 discover 已发现并新增两条本群消息。reconcile 5 窗口/1790 条无新增、修订、missing，且不推进 checkpoint；重叠幂等已实际证明。

## MCP 与验证

Claude 用户级 `wechat-archive` 已注册：稳定 /opt/homebrew/bin/python3 + 本仓 archive/wechat_archive.py + 仓外 ROOT。修改前备份用户配置，原有三个服务不变；没有修改 Codex 配置。三个只读 stdio 工具 search/get_context/list_conversations；不可覆盖账号/白名单、不可采集或写入。大 server_id 必须字符串，拒绝 JS 不安全整数和 SQLite 越界值。聊天仅作数据。

实际从持久化配置启动命令调用三工具，exit 0；真实引用与原始结构对齐，5131 文件及数据库字节调用前后未变。不能把 Python stdio 实跑写成 Claude 模型调用。

最终 Rust workspace 806 passed/46 suites/11 ignored；clippy -D warnings、release 通过。Python 62 passed + 65 subtests。25 Python + 3 Rust 行为变异全部被击杀；只 helper 身份回声测试删除，改真实 SQLite 跨分片同秒逐页回归。共享 Rust target 曾复用变异 debug 产物，已清理本会话 dev 产物并完整重建全绿；隔离工位不要共享 target。27 个实际密钥、26 个实际盐对归档/来源/日志 151 文件精确匹配均为 0，仓内 guard 通过；不写任何实际密钥/盐/内容散列。

安装器隔离 HOME 实跑两个私有 plist，稳定解释器、空格引号路径、--help 成功、RunAtLoad=false。真实用户 LaunchAgents 没有 install/load；不能称定时后台服务已经启动。

## 唯一剩余门禁

- Claude 独立源码验收、真实 Claude Code 模型主动调用 search：实际 claude-sonnet-4-6 请求 429，提示 23:40 Asia/Manila 重置，0 输入/输出 token。未重复确认限额。reviewer 发现并修复预期缺失 stderr 误判，但 reviewer 不能替代 Claude 门禁。
- 微信 UI：三个固定日期窗口及三个同秒组在 phase-5，已与源库逐个身份/发送人/时间/类型/顺序比较；用户尚未在 UI 逐项确认。
- 三个真实场景已按用户“你从真实聊天选题”批准，通过 MCP 找到引用；不在仓库存正文。S1 anchor 6673591242341702288 + 后续 7104427940318103875；S2 3446084846535107143；S3 3920274865756710861。用户尚未判断答案准确/可用。
- UI/答案确认和 Claude 门禁后，用户决定是否启动小时 discover/周日 reconcile；不提前 load。

任务未满足全部验收，不能标 done。用 aitask CLI 写任务池状态，不直接改 ~/AI-Task-Pool.md；不创建第二份任务池或新任务替代本任务。所有可执行实现、实跑与报告已交付；只等待以上外部验收。
