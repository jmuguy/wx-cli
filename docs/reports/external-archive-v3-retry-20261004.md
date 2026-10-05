# 外置盘 v3：额度恢复后独立复验（2026-10-04）

**结论：FAIL / 退回修改；代码尚未完成，禁止真实扩采集或生产切换。当前停止原因是连续返修仍未通过，不再是 GLM 配额。**

任务 `TASK-20261004-075803-13F5`，分支 `feat/archive-layer`，HEAD `601b34ce9ea30bd19d913e903a1213e96d7a5dd5`，全部本轮改动未提交。代码快照 SHA-256 保存在私有指纹清单，算法与 209 文件清单见 `work/external-archive-implementation/retry-reviewed-code.json`。代码变化后本结论不能当作新版本验收。

## 真实模型调用与停止依据

用户通知额度恢复后，16:33 +08 起两个原会话均实际返回 `model=glm-5.3` 并执行读写代码/测试工具。使用原 BigModel 兼容端点、相同会话，不代换模型；继承 MCP 关闭。源端会话 `8215ca10-880d-4083-bd1d-8f88d0747fff`，NAS 会话 `be7e6bde-c777-4770-a82d-8c7bfbe8acf5`。本次未观察到新的 429。

NAS 同一数据库卷绑定问题出现三轮返修失败：

1. 增加连接后路径检查后，替换路径→打开替代库→恢复路径（ABA）仍能绕过。
2. 用进程 fd 扫描证明连接身份后，在真实 Linux 添加一个无关原库 fd，即可让错误连接通过；独立脚本实际输出 `opened_database=replacement`。
3. 改为自定义 ctypes SQLite VFS 后，独立 Linux 最终复验在正常初始化 `PRAGMA journal_mode=WAL` 处报 `attempt to write a readonly database`，连正常链路也未通过。

分别见 `resume2-codex-aba-red.log`、`resume2-linux-binding-probe.log`、`retry-final-linux-binding.log`；完整历史保留在私有工作目录与 GLM 会话记录。成功拒绝一次攻击或 Mac 合成通过不证明 Linux 实现可用。

指定的 `/Users/jmuguy/Documents/Coding/ai-dev-workflow/PLAYBOOK.md` §10 明确要求：“返修回原实施者，审查者不代改；同一改动退回 3 轮仍未通过，停下交人判断”。因此17:00前已中断两个指定 GLM 实施进程、确认退出，冻结当前代码做完整验收；没有继续发出新一轮修补任务，也未由 Codex 代改生产实现。需要用户决定是否把 Linux 卷绑定单独拆成实施任务，重新明确底层依赖/平台与通过条件，再恢复全链路工作。

## 独立验收结果

原始日志统一 `work/external-archive-implementation/retry-final-*.log`，机器汇总 `retry-final-gates.json`。

| 命令/验证 | 结果 | 证据 |
|---|---|---|
| `cargo test --workspace` | PASS：842 passed，48 suites，11 ignored | retry-final-cargo-test.log；exit 0 |
| `cargo clippy --workspace --all-targets -- -D warnings` | PASS | retry-final-clippy.log；exit 0 |
| `cargo build --release` | PASS（仅构建） | retry-final-release.log；exit 0 |
| `python3 -m pytest -q archive/tests` | FAIL：73 passed，1 failed，65 subtests passed | retry-final-pytest.log；exit 1；C0 覆盖漏洞 |
| `scripts/check-no-secrets.sh` | PASS（工具既定覆盖范围） | retry-final-secrets.log；exit 0 |
| 独立 Linux 固定卷脚本 | FAIL：正常 WAL 初始化失败 | retry-final-linux-binding.log；exit 1 |
| `git diff --check` | PASS（已跟踪 diff） | 命令 exit 0 |
| 额外 `cargo fmt --all -- --check` | FAIL：6 个新改 Rust 文件待格式化 | retry-final-format.log；非替代功能门 |

独立评估为 FAIL；11 ignored 不是通过。密钥门不覆盖 Git 历史/忽略日志/仓外；本次未取真实密钥。代码冻结后的测试不复用此前 GLM 自测结论。

## 尚未修复的关键问题

- **P1 / 已复现：C0 可由客户端伪造。** `RunApplier.apply` 直接使用 `run.c0`，endpoint 没有用服务端持久 begin_run 的值替换/核验。独立用例先开始旧 run、后提交新内容，再伪造旧 run 的 C0，最终正文变为 stale content。`test_client_cannot_forge_c0_to_overwrite_newer_capture` 红测保留。必须同时校验 run 的账号/会话/epoch/状态；缺 run 不能凭空成功。
- **P1 / 已复现：Linux 固定卷 VFS 正常写入失败。** 不是缺真实 NAS 才阻塞，隔离 Linux 的合成正常初始化已失败。当前 C ABI 指针/文件名关联元数据及生命周期、匿名临时文件回退、WAL/shm 路由均须独立审查，不能靠 `PRAGMA database_list` 的字符串自证替代行为验证。
- **P1 / 源码发现：S3 完整性未达标。** `archive_inspect.rs` 把正文 BLOB 用 `from_utf8_lossy` 转换；部分已知列未保全；identity 的 shard 仍以表名代替实际 message_N.db；仅图片媒体初步处理，静态快照 manifest 清单/长度/hash 验证与全部类型媒体还未闭环。已给原 GLM 反馈，但冻结时未修复完成。
- **P1 / 源码发现：全链路缺件。** collector、查询投影及独立查询入口、加密备份/基线日志资产恢复、v2 一致迁移、v3 CLI 尚未实现。现有行级 mutation log 多处仅部分指纹，尚不能据此恢复完整状态。完整原始记录仍未在主库/修订链保全。上传清单与 hash 的端到端一致性仍未独立通过。
- **时限未闭环：** watchdog 新增 ACK 握手，但杀进程判断仍为 T_max + 100ms，不能把主动增加预算称为严格 T_max；真实可接受开销未获批准。

## 本次已验证的局部进展

只批准 capture 不默认授予 query、目录 fsync 失败传播、short write、含空格 mountinfo、损坏上传意图拒绝重建、客户端 manifest hash 不匹配拒绝提交等合成行为已独立通过。源快照 CLI 定向曾12通过、Codex快照权限/禁止覆盖2通过；不代替最后完整检查。新增 archive-inspect、上传端点/传输/线协议为部分实现，不能称全量归档完成。

## 环境与授权边界

未读取真实微信正文/密钥，未连接 NAS，未修改现有单群调度/MCP/TCC/签名；不 push、不合并、不提交。所有源/媒体测试均合成数据。仓内 release 构建不覆盖 `~/.local/bin/wx`。

为验证 Linux 专有绑定逻辑，创建了独立 Colima profile `wx-archive-v3-test`（2 CPU、2GB RAM、8GB系统/8GB数据逻辑盘；无宿主目录挂载、未激活 Docker context、未写用户 SSH config）。仅复制新 v3 代码及合成测试至 `/tmp/wx-v3-review`。17:00 已停止并核实，证据 `retry-linux-stop.log`；保留停止的独立测试磁盘供复现，未启动/改变原 default profile。该环境不是实际懒猫设备验收。

全部真实设备身份/加密/独立OS用户/受限SSH/真实源格式/源锁shm与WAL开销/峰值/真实样本/自动调度/全量恢复验收仍 UNVERIFIED。生产门保持，不因单元测试或模型自评放行。后续必须先解决上述代码失败，再呈现完整可审核的真实样本与生产动作范围，按原授权门取得批准。
