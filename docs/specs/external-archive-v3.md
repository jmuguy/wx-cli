# 微信全量归档·懒猫外置盘 v3 实施规格（external-archive-v3）

实施状态（2026-10-05）：**本轮已知失败修复、剩余模块合成实现及独立验收通过；父任务review，真实/生产门未完成**。Rust883通过/11忽略，clippy/release/密钥门通过；Mac Python247通过/35 Linux跳过/65subtests，Linux完整Python281通过/1 Rust工作区缺失跳过，原固定卷34项不改并通过。生产恢复卷绑定路径未实现，CLI默认拒绝；真实源/设备/NAS/人工/Claude门未放行。最新报告 `docs/reports/external-archive-v3-luna-accepted-20261005.md`；下述批准设计与历史报告保留。

状态：设计已批准（五轮对抗审查 `approve_design_only`，`residual_design_objections = []`，2026-10-04，证据目录
`~/Library/Application Support/howie-wechat-archive/external-disk-adversarial-nwhv17k7/`，以 round-5 为准）。
本文档是 **L2 实施规格**：设计冻结之下的代码级实施范围、分阶段交付、证明命令、风险与回滚、未验证硬前置。
它不是实现完成证明，也不是设备/生产验收批准。

- 分支：`feat/archive-layer`，不 push、不改 main、不 commit（实施会话约定）。
- 基线：`601b34ce9ea30bd19d913e903a1213e96d7a5dd5`，工作区原干净。
- 授权边界：只做代码实现、合成数据验证、非破坏性预检；不读真实微信库/密钥/正文，不碰 NAS、调度、TCC、签名、权限、生产配置；数据只落在临时隔离测试目录。生产开关默认关闭（全部新入口都是显式子命令/显式参数，现有命令、定时任务与 MCP 行为零变化）。

## 1. 锁定架构回顾（摘自 round-4/round-5，实施依据）

1. **Mac 读取、懒猫持久归档**：主库、来源证据、附件、修订、FTS、变更日志、查询投影均在外置盘；SQLite 引擎运行于外置盘本地；禁止经 SMB/WebDAV 操作活数据库。
2. **采集与查询范围独立**：保存范围=本账号全部获准的本机群/私聊（含隐藏/折叠）；AI 查询初始仍只允许原群。NAS 接收端再次校验 account/talker/scope。
3. **全部源库走一致性快照**（第五轮锁定，替代一切手工 main/WAL 复制）：
   - 每个源库独立 READONLY SQLCipher 连接（keyspec 与实际 WX 页格式匹配，支持 derived-only 输入）；
   - `BEGIN` + 实际读取建立固定 read view，**同一事务内**完成同 key、同 salt、同 page/reserve/HMAC/cipher 参数的加密 backup（backup API 不做加密→明文转换，不落地全库明文，不 rekey 活源，不 sqlcipher_export 活源，不借旧原地解密缓存）；
   - `SQLITE_DONE` + finish 成功后立即结束源读事务并关闭连接；校验（cipher 完整性/SQLite integrity/结构枚举）在源锁释放后做；只读发布静态加密副本代次；后续枚举/hash/正文扫描由离线只读打开静态副本完成；
   - 禁止 immutable 读活源；失败副本不得当完整 view、不得推进 checkpoint；
   - `T_max` 为源读事务硬时限：有界 nPage 步长、忙即返回、单调时钟检查、**父进程 watchdog** 强杀异常工作者（进程被杀后 OS 释放 fcntl 锁）；超预算丢弃未完整代次、报告该片缺口；连续无法完成则源端门失败；
   - 单读事务保持 WAL read mark、checkpoint 受限、WAL 增长——需用户批准的临时开销；网络/解码/全历史 hash/NAS 等待不进源事务；
   - 源文件前后差异不能直接归因采集器；隔离 fixture + 获准进程级观测确认 worker 只读。
4. **完整性不依赖时间水位**：插入 ID 只作快路；变化/版本不明的分片做全历史流式语义扫描，身份+正文指纹+附件引用与**上一次成功的源观察摘要**比较（绝不与含已删除历史的归档摘要比较）；覆盖迟到消息、手机迁入旧历史、同 ID 编辑、rowid 复用、分片变更、后来下载的旧附件；维护 `MediaNeeds`；保全/可检索/缺口分开报告，机器可读。
5. **上传与耐久确认**：先耐久化上传意图，`upload_id` 传附件到 staging、双侧核验长度/hash，最终 manifest 固定后 fsync；`batch_id` 幂等（同 ID 同内容重放，异内容拒绝）；ACK 丢失查询原批次状态；abort 与 commit 串行 CAS，仅未提交批次可中止；源附件中途消失记真实缺口。NAS 唯一写者：外置盘守卫 → 文件/目录 fsync → FULL 事务（数据+receipt+行级 mutation log）→ `capture_ack`；Mac 收到主盘确认才清本批暂存。验证物理设备/UUID/mount/`archive_id`/epoch/容量；守卫不是启动时看路径存在，所有对象绑定固定目标卷；生产不自动初始化空库、无内置可写 fallback。
6. **AI 查询物理隔离投影**：独立非 root OS 用户读不到 master/来源包/备份/密钥；投影在外置盘，字段白名单，保留 sender/tag/嵌套引用可见性；FTS 只索引可见文本（禁止主库 MATCH 后遮罩）；revision 载荷/撤回原文/raw_xml/来源包默认不入投影；媒体只给 opaque ID；策略缺失、旧 epoch、版本不匹配拒绝查询；撤权先关闭旧查询再发布新投影。文件系统给不出 POSIX/ACL 或经验证的 writer 私有挂载隔离就停，不退回 SQL-only。
7. **对账原子性**：staging 收齐完整 run 后，单写者 FULL 事务一次应用 content/revision/media/missing/FTS/mutation log；C0 并发保护（候选缺失须 `first_commit_seq<=C0` 且 `last_content_or_presence_commit_seq<=C0`）；缺失候选结束前对该会话全部分片再核验一轮新一致 view；拓扑/搬迁不确定不确认；异常大差异转人工（阈值真实规模后批准）；soft missing 不删归档历史、不自动隐藏仍可查询行；无变化观察不重复保留整窗 JSON。
8. **备份与主采集解耦**：主盘 commit 即确认并继续采集，备份离线不停采；独立加密备份=一致性基线+行级状态变更日志+引用资产；`protected_seq` 只推进无空洞连续前缀；捕获新鲜度/缺口与备份滞后/RPO 分报；基线轮换前完整校验+恢复演练；恢复后新 epoch，旧 ACK 无效。
9. **迁移现有单群**：测试独立 `archive_id`；迁移用 v2 一致性全库快照+全部引用 source/assets，不重放 export.json；保留 namespace/历史/revisions/missing；旧数据 `provenance=legacy_visibility_projected`，当时 ignore 配置不可追溯记 `unknown`；补回完整载荷是 `source_upgrade` 不是 `source_edit`；切换前保留现单群任务，最终短暂停旧 writer、同步核验再切；旧 Mac root 只读留回滚。

## 2. 本次实施范围（可独立完成部分）

设备/真实源/外置盘相关的门全部 BLOCKED（见 §6），不影响以下纯代码+合成验证交付。凡未实现或未验证的组件在交接中如实列出，不以 stub/假 guard/自报设备身份/SQL 过滤替代隔离。

### S1 — wx-db 快照核心（`crates/wx-db/src/snapshot.rs`，新增）

- `rusqlite` 增加 `backup` feature（bundled-sqlcipher 已含 backup API，此前未启用——round-5 已核实此缺口并要求实现）。
- 密钥输入二选一：raw key（沿用现有 KDF 派生）或 **derived-only**（`EncKeyPair{key,salt}` 列表，绝不接触 raw key；keyspec `x'<key><salt>'` 天然支持）。
- 单库快照流水线（worker 内执行）：
  1. READONLY+keyspec 打开源，`query_only=ON`；
  2. `BEGIN DEFERRED` + 实际读取（sqlite_master 枚举）建立固定 read view；
  3. 目的连接以**同一 keyspec**（同 key 同 salt）初始化，按源 `page_size`/HMAC 设置 cipher 参数，保证同参数加密副本；
  4. 同一源事务内 backup：有界 nPage 步长、步间单调时钟 deadline、`SQLITE_BUSY` 即返回失败；
  5. DONE+finish 后立即 `ROLLBACK`/关闭源连接释放锁（慢操作不进源事务：无 digest、无网络、无解码）；
  6. 源释放后离线校验副本：同 keyspec 只读重开、`PRAGMA integrity_check`、page_size、表清单与事务内枚举一致、salt 一致；
  7. 校验通过才原子发布（临时文件 fsync→rename→目录 fsync→只读权限），并写机读 per-DB 记录；任一步失败不发布半成品、不推进代次。
- 合成测试（wx-db 单元/集成）：并发写不重启且副本==BEGIN 时刻状态（含 WAL 模式）、derived-only 成功/错 key 失败不发布、超时不发布半成品、副本参数与源一致、只读发布后不可写。

### S2 — wx-cli 父进程 watchdog（`crates/wx-cli/src/cmd/archive_snapshot.rs`，新增子命令）

- `wx-cli archive-snapshot`：对源目录枚举的全部库（contact/session/message 分片/FTS/hardlink/message_resource 等）逐库派 worker 子进程（隐藏子命令 `__archive-snapshot-worker`），逐库墙钟 `T_max`（毫秒，显式参数，执行前写入配置的要求由运维配置承载），超时 SIGKILL、记录缺口、不假称完整；聚合机读 JSON 报告（stdout），任一库失败退出码非 0。
- 归档专用路径：新子命令，不改动任何现有命令行为（生产开关默认关闭）。
- 集成测试（`crates/wx-cli/tests/archive_snapshot.rs`）：合成加密源目录全量快照、worker 超时被父进程强杀且无半成品发布且源锁随进程终止释放、报告机读可解析、失败库明确列出。

### S3 — 离线只读路由 + 完整记录采集（`wx-cli archive-inspect`，新增子命令）

- 只读打开已发布快照集（代次 manifest→相对路径→keyspec 只读连接），归档路径**不再读取 `PersistentCache` 原地解密缓存**（round-5 边界条件：只要还有一个库在读旧缓存，批准作废）。
- **有界按会话完整记录导出**（不是只有 hash 元数据）：按真实 schema（`Msg_<namehash>` 表、`Name2ID`、contact/session/hardlink 结构）分会话、keyset 分页流式导出完整结构化记录——行身份（rowid、server_id、shard）、create_time/sort_seq/local_type/status、`message_content` 全文、`packed_info_data` 及 sha256 指纹、媒体引用枚举。无正文读取就无法交付归档：正文读取只发生在**静态加密快照**上（源事务内零正文扫描）。
- 观察摘要（语义桶比较用）由完整记录派生（身份+指纹+引用），不单独落第二套口径。
- 测试：快照集只读路由（改源文件不影响已发布代次）、按会话有界分页完整导出（大 ID/同秒行保留、页间无丢失无重复）、未知表/列以 unknown 记录不静默跳过、全部读取仅来自快照代次（源文件在导出中被替换不影响结果）。

### S4 — Python v3 协议栈（`archive/v3/`，新包；现有 wechat_archive.py/定时任务/MCP 零改动）

- `guard.py`：外置盘守卫，双档：
  - **生产档（默认）**：入口默认拒绝。要求可证卷身份——解析 Linux `/proc/self/mountinfo`（挂载点、设备 major:minor、fs 类型、设备内 root 路径）、由 `/dev/block/<maj:min>`（或 by-uuid/by-label 映射）反查块设备与 UUID，与配置的期望卷身份（UUID/设备路径/mount root）全量匹配才放行；**固定目录 fd 路径绑定**：打开归档根后持有目录 fd，全部子路径经 `dir_fd` 相对解析（路径被换成 symlink/重挂载后原路径指向别处时拒绝而非重定向）。mountinfo 解析器以 fixture 文本单元测试；macOS 上生产档因平台无 mountinfo 而默认拒绝（如实报 `platform_unverified`），不冒充通过。
  - **合成档（显式 `--allow-synthetic-guard`，仅测试）**：`st_dev` + 标记文件（`archive_id`/`epoch`/创建时设备指纹）绑定，每次开库/事务前复验、容量下限、跨设备 symlink 拒绝、不自动初始化。此档只是合成适配器，**不足以宣称生产 guard**。
- `protocol.py`：持久捕获协议 + **forced-command SSH stdio 传输实现**。线协议（JSON lines 请求/响应）独立于传输：`LocalSubprocessTransport`（本地子进程承载端点，用于合成端到端与懒猫本地部署形态）与 `SshTransport`（`ssh <host> <forced-command>`，同一端点脚本作为 forced command）共用同一实现。角色校验：collector/query 两种角色令牌，端点按角色放行操作子集，未知角色拒绝；客户端重试（指数退避）依赖 `batch_id` 幂等保证安全。流程：耐久上传意图（intent 文件+fsync）→ `upload_id` 分片上传、双侧长度+sha256 → manifest 固定+fsync → `batch_id` 幂等 CAS 提交（同 ID 同内容重放 ACK、异内容拒绝）→ 单写者 FULL 事务（数据+receipt+行级 mutation log）→ `capture_ack`（含 epoch+commit_seq）；abort 仅 pending 态 CAS；ACK 丢失查询批次状态恢复。真实 NAS 设备上的行为 UNVERIFIED（不阻塞代码与本地子进程端到端测试）。
- `master.py`：v3 主库 schema（epoch/commit_seq、消息+可见性+provenance、revisions、media 需求账本、missing 观察、mutation log、批次/receipt、源观察摘要、run 记录），单写者 FULL 事务。
- `collector.py`：消费 S3 完整记录导出 → 语义桶；**身份规则：同 server_id 为稳定主身份；local (shard,rowid) 仅为无 server_id 行的辅助身份，rowid 复用（同 local 身份映射到不同 server_id/内容）绝不覆盖历史**——旧行保留，新行按独立身份入库并记 identity_bind 事件。与**上一次成功源观察摘要**比较产出：迟到消息（含旧历史迁入）、`source_edit`（同 ID 指纹变化）、媒体升级（引用出现/变化）、`MediaNeeds` 更新；仅批准 talker 生成批次；接收端独立再校验 account/talker/scope；本地媒体文件读取端到端（按引用从工作区媒体目录读字节、双侧 hash/长度、有界分批上传 staging）。
- `reconcile.py`：staging 收齐完整 run → 单 FULL 事务应用 content/revision/media/missing/FTS/mutation log；**C0 保护覆盖所有内容更新（不止缺失标记）**：run 的 `run_start_seq=C0`，任何行更新仅当其 `last_content_or_presence_commit_seq<=C0`，否则该行记 conflict 事件留待再捕获，旧捕获不得覆盖后来提交的新内容；缺失候选双条件+跨全部分片新一致 view 再核验；异常差异门（默认 ≥20 且 ≥10%、或 ≥500 转人工——真实规模批准前为可配置默认值）；soft missing 不删历史、不隐藏现行可见行；枚举不完整/未知分片永不发布 missing。
- `projection.py`：从已提交 master 快照（按 commit_seq）生成投影（字段白名单：规范 ID、account/talker、时间、允许的 sender 信息、按 sender/tag/quote 策略裁剪后的当前可见文本、安全类型字段、opaque 资产 ID、missing 元信息、`has_revisions` 布尔）；FTS5 trigram 只索引可见文本；投影 manifest（epoch/policy_version/commit_seq）原子发布。**独立查询入口**：只接收投影根路径（指向 master 根直接拒绝）、epoch/策略版本以服务端投影 manifest 为准（不信任客户端上报）、每次请求前复验 manifest 与撤权记录——**撤权必须实际阻断已打开的旧投影查询会话**（长生命周期会话在下一次请求即失败），权限位测试只证明文件模式、不宣称 OS 用户隔离通过；真实独立 OS 用户切分 BLOCKED。
- `backup.py`：异步备份=一致性基线（master 在 seq B 的一致副本）+行级状态变更日志+引用资产，**实际加密**：采用成熟加密库/工具（Python `cryptography` AES-GCM 流式加密，或 openssl CLI 兜底），密钥材料不落备份明文；`protected_seq` 仅连续前缀推进；恢复演练：解密+校验、基线+日志回放、**引用资产逐项 hash 验证**、恢复到新根、**新 epoch**（旧 epoch 的 capture_ack 全部失效）；捕获新鲜度与备份滞后/RPO 分开报告。
- `migrate_v2.py`：从 v2 库一致快照（v2 sqlite + sources + assets + revisions + missing 状态）生成 v3 run；`provenance=legacy_visibility_projected`；v2 当时 ignore 配置不可追溯 → 审计记 `unknown`；后续全量补回完整载荷记 `source_upgrade`（与 `source_edit` 分开计数）；测试独立 `archive_id`，绝不触碰生产 v2 root。
- `cli.py`：显式子命令入口（`python3 -m archive.v3 …`：init/snapshot/collect/upload/reconcile/project/query/backup/restore-drill/migrate-v2/status），全部要求显式 `--root`/`--archive-id`/密钥参数，不接入 launchd/MCP/现有 CLI。

### S5 — 证据与文档

- 每阶段原始命令输出写 `work/external-archive-implementation/`（忽略由主控写入的 `.git/info/exclude` 精确条目承担，不改根 `.gitignore` 门禁、不新增其他忽略文件）。
- 最终验证命令（§3）+ `AGENT_HANDOFF.md` 更新（文件清单、已验证/未验证、BLOCKED/UNVERIFIED 清单）。

## 3. 证明命令

```bash
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
python3 -m pytest -q archive/tests
cargo build --release
scripts/check-no-secrets.sh
```

阶段内附加（示例，逐阶段记录在 work 日志）：

```bash
cargo test -p wx-db snapshot
cargo test -p wx-cli --test archive_snapshot
python3 -m pytest -q archive/tests/v3_protocol_test.py archive/tests/v3_reconcile_test.py …
```

## 4. 风险与回滚

- **不改生产默认行为**：所有新入口为新增子命令/新包；现有 `decrypt/export/sync-incremental/discover/reconcile/MCP/launchd` 路径零改动；回滚=不调用新命令（代码保留在工作区未提交，用户可整体丢弃）。
- **Cargo 变更风险**：仅 wx-db 的 rusqlite 增加 `backup` feature（同一 bundled-sqlcipher 编译单元，无新依赖源）；`cargo build --release` 全量重验。
- **Python 包风险**：`archive/v3/` 新目录，不 import 进现有模块图；现有 63+65 测试回归确认零影响。
- **误触真实数据风险**：全部测试数据在 `tempfile` 隔离目录；不读真实源、不写真实 ROOT；密钥材料在测试内合成随机生成（无 64-hex 真实密钥形态落盘，check-no-secrets 把关）。
- **假实现风险**：forced-command SSH stdio 协议、角色校验、客户端重试**必须实现**并以本地子进程端到端测试证明（无 NAS 不是不写代码的理由）；真实外置盘/UUID/挂载上的行为、OS 用户切分、TCC 继承、真实 T_max 属 UNVERIFIED/BLOCKED，如实交付（§6），不以权限位测试冒充 OS 隔离、不以合成 guard 冒充生产 guard。
- 回滚粒度：逐文件撤销即可（无 commit）；work/ 日志目录独立忽略。

## 5. 与硬前置的对应（本次不做/做不到的）

见 §6；任何门不满足即停在对应阶段，不缩小范围、不换内置盘、不退回旧缓存、不改 SQL-only 查询。

## 6. 未验证硬前置（BLOCKED/UNVERIFIED，交付时逐项保留）

用户决策类：范围规则、允许的源读锁/shm/WAL 增长开销、`T_max` 实测值、最大捕获延迟、RPO、对账异常阈值、root 级观测许可。
设备门：外置盘访问/映射、物理身份/UUID/mount（mountinfo/UUID/block 解析代码已实现，但真实外置盘上的行为未验证）、文件锁与 fsync 行为、FTS5 可用性、加密解锁、POSIX/ACL 权限隔离、真实 NAS 上受限 SSH 链路（协议代码与本地子进程端到端尚未实现，真实设备行为也未验证）。
源端门：真实 SQLCipher 页/密钥格式核对、同参数 backup 在真实源成功、单读事务 T_max 内完成、无辅助恢复/EXCLUSIVE/自定义 VFS 不兼容、TCC 授权继承、插入游标适用性、CPU/IO/Mac 磁盘峰值 D。
备份门：独立备份目标与故障域、全量恢复证据（真实盘）。

以上任何一项在获准真实验证前，本规格的全部结论仅覆盖合成环境。
