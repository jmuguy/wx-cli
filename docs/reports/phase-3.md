# Phase 3 — 本地归档、增量与对账

日期：2026-10-03。任务：TASK-20261001-173002-8057。批准账号 `wxid_e1xwpk9mdgvf22_1110`，唯一批准会话 `45416093717@chatroom`（哥飞的朋友们）。本报告不包含真实聊天正文、数据库密钥、盐或内容摘要散列。

## 实现与边界

- schema v2；主键 `(account,talker,id_kind,message_id)`。正 server_id 使用 server 命名空间；本地身份使用分片 basename 与数值 rowid。新自动采集缺少必要分片来源即停止，不把不同分片合并。旧原型原始载荷、版本、checkpoint、来源及附件迁移保留，迁移失败回滚。
- 原话与摘要同时可检索；中文 FTS5 trigram，短查询字面 LIKE；包含时间端点。上下文使用一致的复合排序与数值 local_id，不按字符串把 10 排在 9 前面。
- 原始 JSON 和附件按内容寻址保留；附件缓存丢失不删除已归档文件。发送人显示名、摘要、local_id、方向、媒体状态刷新不制造内容编辑。
- 有界整秒窗口、完整 export、重叠补抓。完整性校验拒绝分页残留、skipped、分片警告、未知 stderr 告警、媒体错误及媒体模式不一致。只有成功窗口推进 checkpoint；可信空窗口必须具备精确空结果标记。
- reconcile 先收集全部窗口，再以外层 savepoint 原子导入；后段错误不留下前段修改、missing 标记或 checkpoint 变化。源无库有只标记，不删除。
- 同步锁覆盖 checkpoint 读取、导出、导入及对账，支持同线程嵌套、线程/进程互斥。discover 只用本地 sessions 发现白名单变化，不用 timeline/watch 充当消息归档。

## 获准的分层媒体协议

2026-10-03 用户批准：全部消息及本机实际可用附件归档；已证明的本地缺失显式标记；不联网补下载。只有 typed NotFound/LookupMiss、源文件读时确认不存在或缺少媒体引用可标为 missing。数据库、schema、权限、解码、输出写入等错误仍硬停止。

v1 media manifest 按顶层 Image/Voice/Video/File 消息计数，区分 available/missing/error/metadata_only；重复引用按消息计数，不冒充物理文件数。富卡片仍保留原始结构，嵌套媒体不在此四类型 manifest 的统计范围；附件与微信 UI 的等价性仍待人工核对。`no_media=false` 的采集拒绝 metadata_only 来源，旧二进制无协议也拒绝自动导入。

旧媒体严格模式第一次停止在早期历史窗口；批准后首次分层全历史运行遇目录 `Interrupted system call`，硬停止在 checkpoint `1710491813`，未忽略错误或缩小历史范围。修复完整目录枚举：open 中断重试，迭代中断整目录重启，其他错误保持硬失败。每次 export 只建一个图片索引；运行时输入决定初始化，使用 OnceLock，不添加 once_cell；查找借用最优候选，不分配/复制/重新排序候选列表。

## 真实运行证据

私有配置在仓外，initial_since `1699000614`（最早本地历史），7 天 chunk、3600 秒 overlap、120 秒 settle；媒体未禁用。

- 恢复全历史：134 窗口、65220 次消息导入、65192 新增、0 内容修订、0 missing_in_source；成功完成，未因历史媒体缺失放弃文本。
- 首次完整范围核对：归档 83212 条，`wx-cli query --no-server --limit 1` 的同范围 total 83212，skipped 0。全部真实身份为 server；本地身份跨分片由真实 SQLite 合成夹具验证，不能声称在本群遇到了 local-only 数据。
- 再跑 overlap：导入 1、新增 0、修订 0。30 天 reconcile：5 窗口、1790 条、新增/修订/missing 均 0；349 条可用媒体、11 条明确缺失；checkpoint 保持 `1790992000`，未被对账推进。
- 实际 discover：只返回批准群，导入 3、新增 2、修订 0，证明不是只重复旧数据；同范围源 total 与归档均为 83214，skipped 0。此观察 checkpoint `1790993955`（2026-10-03 10:19:15 +08）。
- 此观察可用媒体消息 4522，缺失媒体消息 6091，metadata_only 0；内容修订 0（revisions 包含 83214 个初始版本）。4512 个去重附件文件、1368953068 字节；逐文件内容散列与寻址目录一致，模式 0600。缺失并不意味着媒体字节完整，不能称历史附件全部恢复。
- 最终 151 个归档 SQLite/原始来源 JSON/私有日志及 CLI 临时文件扫描，已存 27 个实际密钥与 26 个实际盐值精确匹配均为 0。密钥文件 0600、目录 0700；任何值不写入报告。
- 最终 live 分片协议补齐后，实际再次 sync-incremental：导入 2、新增 0、修订 0、可用媒体 1、无缺失/错误；checkpoint `1790994648`。归档数量仍 83214，证明最终代码实际运行，不只有测试通过。

## 回归与变异

实际最终门禁：`cargo test --workspace` 809 passed、46 suites、11 ignored；`cargo clippy --workspace --all-targets -- -D warnings` 通过；`cargo build --release` 通过（artifact 308）。`python3 -m pytest -q archive/tests` 63 passed、65 subtests passed，本次同秒排序修复后已重跑。

先失败后修复的消费行为：派生元数据误计为编辑、同秒 local-only 邻居排序、MCP 越界整数中断后续请求、大 server_id 的不安全 JSON 数字、预期缺失诊断含 os error 2/文件名含 error 被误判、自动采集缺少分片来源。已保留确定性行为回归；删除错误措辞钉死和仅 helper 转发的身份断言，改为真实 SQLite 查询六条同秒/同 rowid/跨分片记录并逐页核对。

Claude 复核新增密钥保存回归：固定临时文件遗留导致 AlreadyExists 已先失败复现，再改唯一私有临时文件原子替换。实际进程 smoke 验证保存/重载、未知遗留文件不变、0600/0700；完整 Rust 门禁与 Claude 修复复核通过，细节见 phase-4。

同秒 server/local 混合排序回归：旧代码真实失败 2 Rust + 1 Python（artifact 298/297），修复为 local_id/rowid 先于 server_id，并同步 SQL LIMIT、双向分页与锚点游标。覆盖同秒跨分片 rowid 重号、正反逐条分页、全扫描/SQL LIMIT、server/序号锚点、混合身份、中文 FTS 与短词 LIKE。实际 release query 三个原组身份集合均不变、skipped=0、无残页；第一组与实际 stdio MCP get_context 均返回 local_id 3626..3635、文字后9图，再接下一文本。MCP 调用前后归档字节/mtime 不变，不修改数据库或历史数据。Claude 新排序评审及原始上下文复核通过，详见 phase-4；第一组 UI 构成获用户确认，另两组及图片内容未验证。

隔离变异结果：25 个 Python + 3 个 Rust，28/28 被对应行为回归击杀；不是编译失败或测试发现错误。

| 变异逻辑 | 结果 |
|---|---|
| local 身份丢分片、重叠重复插入、派生元数据当编辑 | 3/3 red |
| 取消导入 rollback、允许 skipped、允许未完成分页 | 3/3 red |
| 只索引摘要、短词不转义、排除时间下端点 | 3/3 red |
| 对账不标 missing、丢弃旧附件、取消 talker 白名单 | 3/3 red |
| readonly 创建库、取消邻居、checkpoint 回退 | 3/3 red |
| 不记真正内容编辑、取消媒体合同、静默改媒体模式 | 3/3 red |
| 不回退 overlap、任意空结果算成功、发现越过白名单 | 3/3 red |
| 取消外层对账回滚、忽略未知 warning、允许不安全 MCP 大 ID、缺少 live 分片来源 | 4/4 red |
| Rust 实际查询 rowid 归零、中断枚举提前结束、schema/SQLite 错误伪装成媒体缺失 | 3/3 red |

Rust 变异使用了独立源码副本，但曾共享 target，随后主仓门禁错误复用了变异行为的 debug 产物；核对主仓三处源码均未变异后，清理本会话生成的 wx-cli/wx-db dev 产物，完整重建，最终 806 全绿。隔离变异不得共享构建产物；throwaway 工位清理，不保留可误用变异代码。

## 调度与门禁

安装器真实运行于隔离 HOME，含空格/引号路径；两个 plist 均 0600、RunAtLoad=false，渲染命令实际 `--help` exit 0。解释器为稳定 `/opt/homebrew/bin/python3`；没有安装或 load 真实用户 LaunchAgents。每小时 discover、周日 04:17 reconcile 的自动启动仍由用户决定，不声称后台定时归档已经运行。

技术实现与实跑已完成。Claude 独立源码评审及真实模型 MCP 调用均已通过（phase-4）；三个问答的用户业务判断已通过，微信 UI 窗口/同秒核对仍待确认（phase-5）。任务不得标 done。
