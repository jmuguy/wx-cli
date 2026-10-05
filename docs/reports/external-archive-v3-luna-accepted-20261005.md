# 外置盘 v3：代码与合成验收收尾

2026-10-05，父任务 `TASK-20261004-075803-13F5`，分支 `feat/archive-layer`，HEAD `601b34ce9ea30bd19d913e903a1213e96d7a5dd5`。**本轮已知失败已修复，剩余模块的合成实现与独立验收通过；父任务转 review，不标 done，不代表真实全量采集或生产验收完成。** 未提交、未推送、未改 main。

## 实施与保护范围

先读取 AGENTS、交接、前次冻结报告及已批准规格，备份全部既有改动。GLM-5.3 原会话实际恢复并完成因果 claim、行长控制、投影、备份及迁移；10:05 再次返回 429/1308。用户随后明确允许额度耗尽时使用 GPT-6 Luna，两条 Luna 实施分别完成 NAS/CLI 收尾和 Rust 自有组合夹具修复，Codex 只补独立测试、审查与文档。没有把 GLM/CLI 自评或协议 smoke 当成独立验收。

已通过的 Linux 固定卷实现 `guard.py`、`vfs.py` 及原两份固定卷测试与本轮开始备份逐字相同，证据 `luna-final-fixed-volume-preservation.json`；原 34 项继续包含在最终 Linux 全量通过结果中。现有 v2 入口、定时采集、MCP 和真实数据未改动。

最终源/测试指纹 `luna-policy-final-code.json`（213 文件），完整回归后 `luna-policy-final-freeze-verification.json` 显示 changed=[]。全部原始日志、变更前后备份位于私有忽略目录 `work/external-archive-implementation/`，不进入 Git；原失败日志与前次报告保留。

## 最终命令验收

| 门禁 | 实际结果 | 私有日志 |
|---|---|---|
| `cargo test --workspace` | **PASS：883 passed / 11 ignored / 51 suites，exit0** | luna-frozen-cargo-test.log |
| `cargo clippy --workspace --all-targets -- -D warnings` | **PASS，exit0** | luna-frozen-clippy.log |
| `cargo build --release` | **PASS，exit0，仅构建** | luna-frozen-release.log |
| `python3 -m pytest -q archive/tests`，Mac Python 3.14.7 | **PASS：247 passed / 35 skipped / 65 subtests passed，exit0** | luna-policy-final-pytest.log |
| 隔离 Linux 完整 Python 套件，`pytest -q -s archive/tests` | **PASS：281 passed / 1 skipped，exit0** | luna-policy-final-linux-pytest.log |
| 最终投影、采集、CLI 独立/自有组合门 | **PASS：50 passed / 1 Linux-only skipped，exit0** | luna-policy-independent-green.log |
| 实际 CLI forced-query stdio，禁止 master 构造器 | **PASS：hello/search 成功、capture 被拒、exit0** | luna-final-query-cli-stdio.log |
| 密钥门 | **PASS，文档完成后再次扫描非忽略工作区与整个索引，exit0** | luna-policy-secrets-final.log |

Mac 的 35 个跳过是 Linux 固定卷与 OS 用户门，不当作 Mac 通过。Linux 的唯一跳过是 `RustInteropTest.test_real_rust_export_loads_in_python`：隔离 VM 只复制 Python 源码，没有 Rust 工作区/cargo；该实际压缩 Rust→Python 门在宿主完整 Python 与 Rust 套件中通过。Linux `-s` 避免 pytest 自身 FD capture 在测试卷外创建 O_TMPFILE，未修改固定卷测试绕过门禁。

此前 `resume-20261005-final-*` 是 GLM 停止时的中间冻结：Python 230 pass / 2 fail，CLI 缺失，Rust 自有组合夹具失败。较早 `luna-final-*` Python/Linux 结果早于最后投影策略修复；均是历史证据，不替代上述最终结果。Rust 源码/测试在最后投影修复期间保持不变。

## 逐项交付与返修证明

1. **源快照与有界导出 PASS（合成）**：只读一致加密快照、父 watchdog、释放源锁、离线完整记录与精确大 ID、真实分片/稳定 sender；未知 BLOB、多个未知字段合计、整行/page 有界；64MiB 未知宽行在读取前拒绝并通过独立 RSS 上限。WCDB 可选列八种组合通过；错误是 fixture 将明文标为压缩，修成真正 zstd frame，未放宽生产解码。
2. **因果采集与对账 PASS（合成）**：服务端 claim/run/C0 在源视图之前持久绑定；未 claim 的旧历史不能靠时间戳覆盖新内容。实际 Python CLI→Rust snapshot→inspect→collect 链路、旧捕获晚提交、rowid 复用、presence、缺失二次确认、分页、真实合成附件、ACK 丢失恢复与损坏 intent 保留通过。六项独立 Rust 行长/CLI 链门包含在 workspace 全门。
3. **投影与权限 PASS（合成）**：raw XML、嵌套引用、隐藏 sender/quote/tag、revision 与来源路径不入可读数据/FTS；媒体使用 opaque ID。原 XML title 路径/子树漏洞已逐项先红后绿。Linux 实际 nobody 可查询 projection，不能打开 master，也不能列 master 根；只证明隔离 VM，不证明 NAS 部署。
4. **策略撤权 PASS（合成）**：最终审查另复现两项红测：收紧策略但尚未重建时 fresh/stateless reader 可读旧代次；同版本不同内容不阻断旧 reader。最小修复后，策略内容变化要求严格增加版本；活策略与已发布 manifest 版本不符时，新旧所有查询均拒绝，直到匹配代次发布。同版本同内容仍可幂等刷新。红/绿证据 `luna-policy-independent-red.log` / `luna-policy-independent-green.log`。
5. **备份与恢复演练 PASS（合成）**：实际流式 AES-GCM 基线/增量、行级 after-image、11 表及 mutation log 连续前缀、全部引用 source/assets 字节恢复、FTS 一致性、新 epoch/旧 ACK 拒绝；篡改密文不发布 marker；已有 writer lock 不丢失；独立加密目标及外部 TMPDIR 不落明文；目标祖先/叶 symlink 拒绝；乱序发布不倒退 protected_seq。12 项独立备份门在 Mac/Linux 全门通过。运行依赖 `cryptography`；缺依赖明确失败。
6. **v2 迁移 PASS（合成）**：由实际 v2 importer 生成的数据库一致快照迁移，保留 server/local namespace、历史修订、missing、全部引用 source/assets、legacy provenance/unknown 忽略配置；原文件 hash 不变。CLI 迁移入口亦实跑通过。
7. **角色 CLI / 协议 PASS（合成）**：`python3 -m archive.v3` 的 init/snapshot/collect/upload/reconcile/project/query/backup/restore-drill/migrate-v2/status/serve 使用实际函数链。独立门验证 pending 上传及 prepared run 真提交、重复投影刷新、查询隔离、加密恢复、迁移、默认生产拒绝、强制角色拒绝另一有效令牌、query 拒绝 master auth 路径、端点退出码保留。stdin/stdout 均在读取时限制 8MiB 帧；超长输入关闭会话，不重解释后缀；卷故障 fatal 立即停止。LocalSubprocess 与 SSH stub 均通过，未连接真实 NAS。

## 尚未完成的门与授权

- **BLOCKED / 未实现生产恢复路径**：当前 `restore-drill` 恢复产物使用合成 marker，只允许显式 `--allow-synthetic-guard`。生产默认以 `production_restore_unverified` 拒绝，未静默选择合成守卫；不能把合成演练称为生产恢复能力。上线前需完成生产恢复卷绑定路径及独立设备验收。
- **UNVERIFIED**：真实微信页格式/源读取时限、WAL/内存/空间开销、大规模吞吐；真实 NAS/SSH forced command、外置盘 UUID/挂载/加密、fsync/锁/O_TMPFILE、writer/query UID/GID/权限部署；真实 v2 迁移一致性、手机迁入产物兼容性、实际生产备份恢复。
- **UNVERIFIED，分别保留**：v3 真实群聊/私聊样本与微信 UI 人工核对、用户问答业务验收、Claude 独立评审、实际 Claude 模型工具调用。旧 v2 的通过记录不迁移为 v3 通过，协议 smoke 不替代这些门。
- **待用户明确批准**：真实范围盘点/扩大采集与生产切换。没有读取新真实微信库/密钥/正文、没有扩大白名单、没有连接生产 NAS、没有改 launchd/MCP/TCC/微信签名/真实用户权限。后续先明确账号/会话范围、设备身份、权限/加密、源开销、恢复与回滚，再申请真实执行批准。

隔离 `wx-archive-v3-test` 仅合成数据、无宿主挂载、autoActivate=false、sshConfig=false；收尾只停止该 profile，保留测试盘。default 的运行状态仅观察，不操作。最终环境与停机证据 `luna-policy-final-environment.json` / `luna-policy-final-vm-stop.log`。
