# wx-archive 交接

更新：2026-10-05。父任务 TASK-20261004-075803-13F5；分支 feat/archive-layer。v3代码已提交/推送并部署至既有懒猫主机，实机合成验证通过；真实采集服务未启用，生产恢复与真实/人工门仍未完成。下方保留原v2历史验收和启用记录。

## 2026-10-05 代码已提交、推送、部署，未启用真实v3采集

代码提交 `c9984532a8ee9b2d928cebf1eeebaf1c9ef906c1` 已推送到 `origin/feat/archive-layer` 并核对远端一致；main未改。goldmine既有SSH通路与远端/AGENTS.md、数据目录README已核对。安装目录 `/root/.local/share/howie-wechat-archive-v3/releases/c9984532a8ee`，`current`指向该版本；这是独立代码部署，非外置盘归档数据目录，也不是生产查询用户的最终runtime部署位置。

目标Linux x86_64/Python3.11.2/SQLite3.40.1，FTS5 trigram可用。系统缺ensurepip与cryptography，采用版本目录私有venv+离线wheel安装cryptography49.0.0/pytest9.0.2，没有安装系统包或修改系统Python。源码包SHA256与266个提交文件逐项一致；实际目标机33项CLI/stdio/备份/迁移/投影合成测试全部通过，current CLI help成功。未初始化真实主库、未配restricted SSH角色/生产OS账户、未创建或启用采集服务，现有Mac v2保持原状。

下一步：补齐production restore卷绑定路径；确定外置盘archive root、UUID/挂载/加密与writer/query账户并验证恢复；获准后做有限真实样本、微信UI/问答与Claude独立/实际工具验收；再做真实范围清单、分批全量与明确批准的迁移切换/定时触发。父任务仍review，不把代码部署记作全业务上线。见 `docs/reports/external-archive-v3-deployment-20261005.md`。

## 2026-10-05 用户追加授权：提交、推送与部署

用户明确要求“直接提交、推送、部署”。这次授权覆盖feat/archive-layer的当前v3改动提交/推送与版本部署，覆盖本任务先前“不提交/不推送”的约定，不改main。沿用已验证冻结代码；现有报告中的“未提交/未推送”保留为当时历史事实。

部署与真实采集启用分别记录：交付版本不能自动扩大白名单、初始化真实主库、重启现有采集任务或切换生产采集链。生产恢复路径未实现、真实设备/人工/Claude门仍未完成。已通过现有lzc/SSH配置确认goldmine，并完成独立代码版本目录部署；没有创建或启用真实采集服务。

## 2026-10-05 收尾：GLM + 用户批准的 Luna，合成门通过，待真实门

GLM-5.3原会话恢复后完成因果claim、有界行读取、projection/backup/migrate；再次429/1308后，按用户明确指示用GPT-6 Luna完成角色CLI、stdio有界读取/fatal终止、Rust压缩组合夹具及最终投影策略漏洞返修，Codex独立验收。既有修改与失败证据全部保留。guard.py/vfs.py与原两份Linux固定卷测试逐字不变；原34项复验通过。

最终冻结独立结果：Rust883通过/11忽略/51套件，clippy/release通过；Mac Python247通过/35 Linux专用跳过/65subtests通过；Linux完整Python281通过/1 Rust工作区缺失跳过。实际CLI→Rust claim/snapshot/inspect/collect、prepared upload/reconcile、projection刷新及query隔离、AES-GCM完整/增量恢复、实际v2 importer迁移通过。最终撤权红测证明fresh/stateless reader漏洞，已修成策略版本错配阻断所有查询至重新发布，同版本内容变化拒绝。文档后密钥扫描通过。

父任务转review，**不标done/生产完成**。production restore卷绑定路径尚未实现，CLI默认拒绝，仅显式synthetic演练；真实源开销/页格式、NAS/SSH、盘UUID/加密/锁/fsync、部署OS权限、真实迁移恢复、微信UI/问答、Claude独立评审/实际模型工具调用分别UNVERIFIED。真实盘点/扩大采集与生产切换仍须用户明确批准；现有v2定时/MCP/真实数据保持原状。仅停止本轮隔离wx-archive-v3-test，不操作default。

完整清单、原始日志与授权边界见 `docs/reports/external-archive-v3-luna-accepted-20261005.md`；最终指纹 `work/external-archive-implementation/luna-policy-final-code.json`，验收后changed=[]。继续前读本节及最新报告，不能再按历史报告把模块当缺失，也不能把合成通过当真实通过。

## 2026-10-04 23:54 续接：额度仍阻塞，既有成果保留

用户明确接续父任务。现有40个修改/未跟踪文件已在私有work目录备份；本轮GLM三个原会话均实际API 429/1308，没有新实现，逐文件核对与备份无变化。独立Linux固定卷34/34再次PASS；Python147通过/5失败/34Linux跳过/65subtests，与冻结报告一致。Rust代码未变，本轮未重复其全门，不把旧结果当本轮运行。服务仍提示2026-10-05 01:17:46 +08重置，仅提示未验证。

只停止本轮拥有的GLM实施进程，隔离VM收尾；未改真实数据/NAS/调度/MCP/TCC/签名/权限，无提交推送。父任务仍BLOCKED/未完成，未代写实现或换模型。完整续接证据、剩余项和原分工见 `docs/reports/external-archive-v3-resume-20261004.md`；前次报告与固定卷验收保留。

## 前次状态：2026-10-04 23:30 后冻结（GLM再次额度中断）

**固定卷子任务已done/PASS，父任务blocked/未完成。** 用户恢复额度后GLM实际完成了固定卷子任务，Codex验收通过才恢复父任务。本次冻结独立结果：Linux34/34；Rust865通过/11忽略/50套件，clippy/release通过；Python147通过/5失败/34Linux跳过/65subtests通过。报告 `docs/reports/external-archive-v3-after-binding-20261004.md`。

父任务尚缺：未claim快照仍可凭时钟覆盖已有内容（2项独立红测）；另3项GLM自测失败；Rust整页缓存/未知列行总上限待修；projection/backup/migration/CLI均未生成。最后代码指纹 `work/external-archive-implementation/parent-quota-frozen-code.json`，未提交/push，无生产改动。

实际429/1308提示2026-10-05 01:17:46 +08重置，仅服务提示未验证。全部GLM已停止，隔离VM收尾状态见报告环境证据。续做分工：NAS会话负责除projection外的Python；原源端会话转projection独占两文件；新3995f1d8会话只限Rust行读取。各提示/会话/门禁详见上述报告，恢复前先读，不互相覆盖。已批准代码权限持续有效，真实源/NAS/生产门仍未批准。

## 父任务续做检查点（2026-10-04 23:14 +08，仍实施中）

Codex独立Rust复验：860 passed / 11 ignored / 50 suites，clippy/release exit0，日志 `parent-codex-final-*.log`，指纹 `parent-codex-final-rust-code.json`（均在私有work证据目录）。首次独立测试发现fixture依赖umask077，GLM已显式设0600修复，未放宽生产规则。源端现继续补读取/解压前容量上限和图片原件/缩略图/派生物标识，因此上述结果绑定补改前指纹，后续须重验受影响门。

NAS C0/run绑定、presence、资产、分页行长、ACK丢失恢复、损坏intent共20项Codex独立测试通过；随后新端到端用例 `test_delayed_older_snapshot_cannot_replace_newer_capture` 失败：旧export晚collect被分配新C0，覆盖新内容。GLM正修源视图之前取得run/C0的因果绑定（`parent-capture-order-red.log` / `parent-capture-order-review.txt`）。不得把20项通过称全链路C0正确。当前采集器已分页、流送媒体、持久intent；projection/backup/migration/CLI未交付。实际GLM源/NAS会话均继续，所有数据合成，真实生产门不变。

## 父任务续做检查点（2026-10-04 22:36 +08，仍实施中）

两条实际 GLM-5.3 原会话续做 Rust 源端与 Python NAS。Codex新增 run 绑定、未变化 presence、附件声明独立测试，当前16项全过；完整 Mac Python 独立复跑101 passed / 34 Linux skipped / 65 subtests passed，exit0，证据 `work/external-archive-implementation/parent-python-c0-green.log`。Rust新增独立CLI漏片/跨库身份红测待修，证据 `parent-source-independent-red.log`；两端正统一record contract v2。collector/projection/backup/migration/CLI和端到端仍待交付；不能把中途测试通过当完成。父任务仍claimed，固定卷子任务done。生产门不变。

## 2026-10-04 Linux 固定卷子任务已通过，父任务恢复

子任务 TASK-20261004-174427-7D96 经实际 GLM-5.3 修复、Codex 独立验收：Linux 合并34/34、独立18/18通过，匿名临时文件同卷及12MB数据读写证据通过。详见 `docs/reports/linux-volume-binding-accepted-20261004.md`。Mac Python仍有父任务已知C0失败；不代表全项目通过。父任务 TASK-20261004-075803-13F5 恢复源端完整性/C0/全链路实施，真实源/NAS/生产门不变。隔离 wx-archive-v3-test 继续用于合成验证。

## 2026-10-04 21:42 Linux 固定卷再次续做中

用户通知额度恢复，原GLM-5.3会话已实际响应并修复ENOTDIR错误分类/临时名称缓存增长。Codex最新合并Linux23项通过，但新增temp名冲突故障注入发现：base unixOpen独占创建EEXIST后只读回退，导致同名合成文件被删除。已实际strace复现并退回GLM封闭此失败分支；子任务仍未放行，父任务不恢复。证据`binding-retry-temp-race.log`、`.strace`和`binding-retry-linux.log`；只测试独立VM合成数据。

## 2026-10-04 18:12 Linux 固定卷独立任务：待GLM额度恢复

- 子任务 `TASK-20261004-174427-7D96`（父TASK-20261004-075803-13F5），用户批准先拆分、通过才继续。GLM-5.3已实际修复VFS原始指针生命周期、守卫stat避免释放SQLite锁、Linux禁止注册失败回退、临时spill；Codex只独立测试/文档，未代改实现。
- 独立Linux13项全部通过；合并GLM自测20项中19通过/1失败：中间目录symlink实际拒绝，但返回path_open_failed，测试期待其他代码。未放行，父任务保持暂停。
- Rust842通过/11忽略、Clippy/release通过；Mac Python73通过/1失败(C0原有)/20 Linux跳过/65subtests。密钥门曾因两份报告中的完整SHA256指纹失败，现改为引用私有清单且不改扫描门，最终重验见报告。
- 18:08 GLM实际429/1308，服务提示19:57:59重置、进程exit1，无后台实施；只报告提示时间，不假称额度恢复。独立Colima wx-archive-v3-test与原default均Stopped。
- 规格 `docs/specs/linux-volume-binding.md`，报告 `docs/reports/linux-volume-binding-20261004.md`，证据 `work/external-archive-implementation/binding-*`。当前代码仅guard/master/vfs及测试变化；源端/C0/全链路其他缺口未改。未触碰真实源/密钥/NAS/调度/MCP/权限，无提交/push。
- 恢复后先让原GLM解决剩余错误码/测试语义并补最终自验，Codex重审通过再恢复父任务；既有代码授权持续有效。真实设备/生产门不变。

## 2026-10-04 外置盘 v3 暂停返修（验收 FAIL，禁止切换）

- TASK-20261004-075803-13F5：codex 领取、实际 GLM-5.3 实施、Codex 独立复验。基线 `601b34ce9ea30bd19d913e903a1213e96d7a5dd5`，全部改动未提交，不 push、不改 main。
- 用户通知额度恢复后，16:33 +08 两个原 GLM 会话均实际响应并续写代码，未再见429。源端 SID `8215ca10-880d-4083-bd1d-8f88d0747fff`、NAS SID `be7e6bde-c777-4770-a82d-8c7bfbe8acf5`。已停止这两个实施进程并确认退出，不再后台继续。
- 冻结代码独立结果：Rust workspace 842 passed / 48 suites / 11 ignored；Clippy/release/密钥门通过；Python 73 passed / 1 failed / 65 subtests passed。失败是客户端伪造 C0 导致旧捕获覆盖新内容。Linux 卷绑定正常 WAL 初始化也失败。额外 fmt check 有6文件格式差异。完整命令/证据/代码指纹见 `docs/reports/external-archive-v3-retry-20261004.md`。
- 暂停原因是指定 PLAYBOOK §10“同一改动退回3轮仍未通过，停下交人判断”：路径检查 ABA、无关 fd 欺骗连接证明、新版 ctypes VFS 正常写失败均已复现。建议用户决定是否将 Linux 固定卷层独立拆任务再续做；本次不是额度阻塞，不得自动宣称完成。
- S3 导出新增但尚有正文 BLOB 有损/分片身份/媒体完整性缺陷；S4 仅部分 master/guard/reconcile/upload/transport/VFS，collector/projection/backup/migration/CLI 与端到端均未完成。完整记录与可重放日志待补。watchdog ACK 握手已加，严格 T_max 仍未证明（主动100ms slack）。
- 批准范围保持代码/合成/非破坏性预检；未读取真实正文/密钥，未连接 NAS，未修改现有单群调度/MCP/TCC/签名。真实范围、设备、权限隔离、源开销、备份恢复和生产触发全部未验收。源码使用仓内 target/release/wx-cli，不覆盖 ~/.local/bin/wx。
- 独立 Linux Colima profile `wx-archive-v3-test` 已停止；无宿主目录挂载、不激活 context、不改 SSH config，原 default profile 仍 Stopped。保留隔离测试盘用于复现。原始证据 `work/external-archive-implementation/` 私有且忽略；历史14:55配额报告保留但已标历史，不代表当前状态。

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
