# 固定卷通过后的父任务续做与冻结验收

> 本文是2026-10-04历史冻结。2026-10-05按用户批准以GLM续做、额度耗尽后GPT-6 Luna收尾；最新结果与仍未完成的生产/真实门见[本轮合成验收](external-archive-v3-luna-accepted-20261005.md)。下文原始状态和失败证据保留。

2026-10-04，分支 `feat/archive-layer`，未提交、不 push。**Linux 固定卷子任务 PASS / done；父任务 BLOCKED / 尚未完成。** 本次用户通知额度恢复后，实际 GLM-5.3 成功续做，Codex 独立验收；并非额度一直未恢复。

## 已完成的独立子任务

`TASK-20261004-174427-7D96` 已通过并关闭，报告见 [Linux 固定卷独立验收](linux-volume-binding-accepted-20261004.md)。按用户指示，通过后才恢复父任务 `TASK-20261004-075803-13F5`。父任务改动后再次在隔离 Linux 上复验34/34通过，未将 Mac 的 skip 当成 Linux 通过。

## 父任务实质进展

GLM 原源端会话完成静态快照清单逐项校验、真实数据库分片身份、精确字符串 server_id、v2 原始列无损记录与压缩正文、Mac 侧媒体解码/错误硬失败、私有图片密钥文件输入、严格 watchdog 时限、媒体读取/解压上限与原件/缩略图标识。Rust 与 Python 的实际压缩正文导出联调通过；仅缩略图存在时明确不冒充原图。

NAS 会话修复服务端 run/C0 绑定、scope/kind/status/epoch 检查、无变化 presence 推进、附件清单与实际对象核验；加入分页接收、单事务应用、流式附件上传、本地持久意图、丢失 ACK 的同批恢复和损坏意图保留。Codex 独立故障测试曾先红后绿，原始日志保留。

这些进展不等于全链路完成。后续独立测试证明：未 claim 的旧视图仍能靠新的服务端 run 和源时间戳覆盖已有内容；即使时间戳相同或更大，也无法证明真实因果次序。已要求先领取 run/C0 再取得源视图，并在服务端保存权威 claim 标记；额度中断时这一修复尚未完成。

## 冻结后的实际验收

所有原始证据在私有忽略目录 `work/external-archive-implementation/`。代码/测试指纹为 `parent-quota-frozen-code.json`；测试前后核对无代码变化，见 `parent-quota-freeze-verification.json`。完整指纹不写入公开文档，不改变密钥扫描规则。

| 门禁 | 实际结果 | 日志 |
|---|---|---|
| Linux GLM+Codex固定卷测试 | **34/34 PASS**，含Codex18项，exit0 | parent-quota-linux-binding.log |
| `cargo test --workspace` | **865 passed / 11 ignored / 50 suites，exit0** | parent-quota-cargo-test.log |
| `cargo clippy --workspace --all-targets -- -D warnings` | **PASS，exit0** | parent-quota-clippy.log |
| `cargo build --release` | **PASS，exit0，仅构建** | parent-quota-release.log |
| `python3 -m pytest -q archive/tests` | **147 passed / 5 failed / 34 skipped / 65 subtests passed，exit1** | parent-quota-pytest.log |
| 密钥扫描 | **PASS，文档写入后再次独立exit0** | parent-quota-secrets-pre-docs.log、parent-quota-secrets-final.log |

5项Python失败必须保留：

- Codex `test_unclaimed_view_clock_cannot_authorize_overwrite[200]`、`[300]`：同秒/时钟回拨情形没有因果凭据，仍覆盖已有内容。
- GLM `PagedCollectTest.test_oversize_previous_observation_walked_in_chunks`：分页调用断言不满足。
- GLM `MissingSemanticsTest.test_real_missing_published_only_via_second_recheck`：`run_invalid: row op #0 identity malformed`。
- GLM `MediaTaxonomyTest.test_permission_error_is_hard_failure`：fixture媒体路径不存在（FileNotFoundError），未达到权限行为验证，不能当成通过。

首次独立Rust复跑还发现自有key-file测试依赖启动器umask077；GLM显式把fixture设为0600后，在普通宿主环境重跑通过。生产权限拒绝未放宽。历史失败证据 `parent-codex-cargo-test.log` 保留。

## 尚未完成，不能跳过

1. **捕获/对账因果性**：权威claim必须在源视图之前，绑定 account/talker/epoch/run；未claim的已有历史不能凭秒级时钟覆盖或发布missing。claim后的实际Rust子进程链路仍需验收，不能只用手写fixture。
2. **源端原始行总上限**：虽然媒体和解压已有上限，`archive_inspect.rs`仍先缓存整页 `page_rows`，未知BLOB/TEXT和整行总长度没有SQLite读取前的统一上限。追加Rust任务尚未实施；需逐行处理、SQLite行长度限制及未知大字段fixture。日志/提示 `parent-row-bounds-task.txt`。
3. **剩余模块**：`projection.py`、`backup.py`、`migrate_v2.py`、`cli.py`均未生成。独立查询OS用户、字段/标签/嵌套引用过滤、撤权关闭旧reader、独立加密备份完整恢复、v2一致性迁移、forced-command角色CLI及完整端到端均未通过。
4. **真实环境**：真实源页格式/读取开销/峰值、真实磁盘身份/挂载/加密/O_TMPFILE/锁/fsync/权限、真实NAS传输、真实群人工核对、生产切换全部未验证。没有读取真实微信正文或密钥、没有连接生产NAS，没有改调度/MCP/TCC/签名/实际用户权限。

## 再次额度中断与恢复分工

23:30 +08，源/投影与NAS两条实际会话返回 **429 / 1308**，服务提示 **2026-10-05 01:17:46 +08** 重置。该时间仅是服务提示，未验证一定恢复。证据 `parent-projection-glm.log`、`parent-nas-remaining.log`。追加原始行限长会话在连续API错误时被主控停止，没有有效实施交付，不冒称已调用成功或完成。

所有实施进程已停止；隔离 `wx-archive-v3-test` 停止结果单独留在 `parent-quota-vm-stop.log`，最终环境状态见 `parent-quota-environment.json`，原default不启动。保留合成测试磁盘以便续做。没有切换模型或由Codex代写实现。

恢复时保持文件互斥：

- NAS SID `be7e6bde-c777-4770-a82d-8c7bfbe8acf5`：先解决因果claim及5项Python失败，再做backup/migrate/cli和集成。提示 `parent-nas-partition.txt`、`parent-capture-order-review.txt`。
- 原源端/现投影 SID `8215ca10-880d-4083-bd1d-8f88d0747fff`：当前只拥有 `archive/v3/projection.py`、`archive/tests/test_v3_projection.py`、私有 `projection-contract.md`；提示 `parent-projection-task.txt`。不再改Rust。
- 追加Rust SID `3995f1d8-8fd5-4658-8d57-674136198c40`：只修新归档行读取上限和自己的Rust测试；提示 `parent-row-bounds-task.txt`。首次请求未有效完成，恢复入口须使用resume或新会话，不能重复已有session-id创建。

已批准的代码/合成验证范围可继续，不需重复授权。真实扩大采集与生产切换仍按原交接展示具体范围、设备身份、加密/权限、源开销及备份恢复后取得用户明确批准。
