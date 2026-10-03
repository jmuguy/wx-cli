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

初次持久配置 stdio 协议实跑 exit 0、文件不变。2026-10-03 用户确认 Claude 额度恢复后，claude-sonnet-4-6 模型已实际调用 search/get_context 并核对同一 message_id 3920274865756710861，进程 exit 0。首次上下文大数字误传被拒绝，一次回退采用 message_id 字符串 + id_kind=server，两工具均成功；归档字节/mtime 不变。原始事件仅仓外 0600 保存，报告无聊天正文。

同秒排序验收时 Rust workspace 809 passed/46 suites/11 ignored；clippy -D warnings、cargo build --release 通过。Python 重跑 63 passed + 65 subtests。同秒排序新增 2 Rust/1 Python 消费行为回归已先失败后通过；实际 release 普通/锚点查询与 stdio MCP 第一组文本后9图一致，三组身份集合不变、skipped=0、无残页，MCP 库字节/mtime 不变。既有25 Python +3 Rust变异28/28击杀记录保留，本次不重复宣称新变异。既有27实际密钥/26实际盐对151仓外文件精确匹配0和仓内guard证据保留；不写值。隔离Rust变异不得共享target。后续启用排查撤回后的最终门禁及删除两项非业务测试的说明见下方与phase-3。

安装器隔离 HOME 的模板/路径验证保留。2026-10-03用户明确授权启动，真实两个LaunchAgents已安装0600并实际bootstrap/kickstart；首个小时新增35。2026-10-04处理单次AppData弹窗后小时新增23、30天周对账复核1779条且无消息差异，但新进程仍提示，曾停用等待持久系统授权。用户随后明确给准确Python.app打开完全磁盘访问；私有plist直接执行稳定opt路径下Python.app/Contents/MacOS/Python，TCC主体org.python.python与授权匹配。全新小时51261（07:21:02）和周52365（07:24:21）均exit0、新stderr0字节，TCC分别3/8次AllFiles允许、授权提示均0。两个任务已加载并enabled，当前空闲，每小时整点discover、周日04:17近30天reconcile。证据ROOT/scheduler-activation-875zzki8/scheduler-enabled.summary.json及两份tcc-enabled日志，详见phase-3。

## 验收完成与定时采集启用

- 两个 Claude 条件现均通过：真实模型 search/get_context 成功；全量源码输入曾在 900 秒超时，一次回退评审全部 19 个生产文件差异，发现一个 medium/P2 固定临时文件阻塞保存。真实回归先报 AlreadyExists，改唯一 NamedTempFile 后通过；实际进程 smoke 确认后续保存/重载、遗留文件不变及 0600/0700。Claude 针对最终修复及两个上下文疑点复核 verdict=pass、findings=[]、unverified=[]。详见 phase-4；旧 429/超时仅为历史记录。
- 新同秒排序修复的独立 Claude 评审/原始上下文复核亦通过：三项初评误报已撤销，仅跨分片 rowid 重号的确定性兜底说明为 info、无可证缺陷。评审目录 ROOT/claude-order-review-bpxw_aah；完整Rust809/Python63+65与实际CLI/MCP证明见phase-3/4。
- 微信 UI：day三个窗口用户反馈正常；第一组文字锚点日期时间及“文字→连续9图→下一文字”构成确认，排序已修复。提供三份新表 ROOT/ui-acceptance-source-order-ct_ewa2u 后，用户明确确认另两组9条撤回/7张图片“都是对的”，三组消息构成均通过，不再阻塞G5。旧失败表保留。图像本地缺失字节与图片内部顺序未验证，不声称历史媒体字节完整。
- 三个真实场景已按用户“你从真实聊天选题”批准，通过 MCP 找到引用；不在仓库存正文。S1 anchor 6673591242341702288 + 后续 7104427940318103875；S2 3446084846535107143；S3 3920274865756710861。2026-10-03 重新提供三个问题和答案后，用户明确确认原话一致、没有遗漏关键条件、可以实际回答问题；问答业务验收通过，不扩大为 UI 确认。
- 调度启用与准确Python.app的macOS完全磁盘访问均由用户明确批准。新运行任务TASK-20261003-220942-6344的无弹窗双后台运行、持久启用和私有权限验收通过，阻塞已解除；两个任务保持加载启用。源码/资源类型的试探改动未解决后台问题，已全部撤回；最终Rust807/46suites/11ignored、clippy/release通过，仅移除两项终端标签文案断言，其中一项依赖真实微信状态并挂起；保留JSON契约/业务回归，实际paths --json与只读archive status成功。最终status83272条、checkpoint1791067219，可用媒体消息4537、明确源缺失6091；真实keystore旧mtime未变，本次双后台运行前后哈希不变，MCP/已有归档继续可读。

归档/只读MCP原任务TASK-20261001-173002-8057已完成，G5通过；后续定时启用单独任务TASK-20261003-220942-6344亦验收完成。账号和单群白名单不变、不联网补媒体；系统权限由用户设置，没有修改TCC数据库或微信。全部状态用aitask CLI写唯一任务池，不直接改文件。私有plist解释器为 `/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python`；安装器重跑会恢复PATH中的python3选择，Python升级可能改变签名，届时须保留并重新核对直接Python.app配置、系统权限与新进程无提示运行。当前不留待处理的启用步骤。
