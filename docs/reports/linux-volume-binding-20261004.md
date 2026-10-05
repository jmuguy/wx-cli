> 历史记录：已由 [最终独立验收](linux-volume-binding-accepted-20261004.md) 取代当前状态；以下保留当时失败证据。

# Linux 固定卷绑定独立验收（2026-10-04）

**状态：未完成 / 尚不放行。独立13项Linux行为测试通过；合并20项Linux测试仍1项失败；GLM再次配额中断。父任务不恢复。**

子任务 `TASK-20261004-174427-7D96`；父任务 `TASK-20261004-075803-13F5`。用户明确批准先拆分此任务、GLM完成并由Codex验收通过再继续父任务。规格 `docs/specs/linux-volume-binding.md`；分支 `feat/archive-layer`，HEAD `601b34ce9ea30bd19d913e903a1213e96d7a5dd5`，未提交、不push。

## 实施与实际范围

GLM-5.3沿原NAS会话 `be7e6bde-c777-4770-a82d-8c7bfbe8acf5`实际执行；读取源码、改写绑定并新增自身测试。Codex只写独立测试/规格/报告/交接、运行独立验收，未代改生产实现。

对照父任务冻结指纹，原有代码仅 `archive/v3/guard.py`、`master.py`、`vfs.py` 改变。新增GLM测试 `archive/tests/test_v3_volume_binding.py`、Codex测试 `archive/tests/test_codex_linux_binding.py`。Rust/C0/其他归档模块未修改。

本次v3代码及相关测试快照SHA-256保存于私有指纹记录；文件清单/算法输入见 `work/external-archive-implementation/binding-reviewed-code.json`。

## 根因、修复和证据

1. 原WAL初始化错误实测扩展码 `1032 SQLITE_READONLY_DBMOVED`。ctypes的c_char_p把SQLite持有的文件名指针转成临时bytes，unixFile随后使用失效路径。GLM改成原始指针透传，在xFullPathname生成固定root fd路径，保留SQLite文件名关联数据的生命周期。
2. 原guard.prove_db反复打开并关闭主库，会释放进程在该inode上的POSIX锁。独立默认SQLite对照先输出 `SQLite_only=blocked`，额外open-close后输出 `after_unrelated_fd_close=acquired`（`binding-posix-control.log`）。GLM改为沿固定目录链stat，不打开/关闭数据库叶文件；独立跨进程锁保持测试转绿。
3. Linux VFS注册失败原来会退回普通路径，独立红测失败；GLM现直接传播注册错误，Linux合成档同样不回退。
4. 临时spill原来发生bytes/str拼接、不可hash的ctypes缓冲区错误，被回调吞成SQLITE_CANTOPEN。GLM修复后，FILE temp_store+小cache下12个1MB blob成功写入和读回，并检查临时文件保持在绑定目录；生成名称缓存改为VFS实例持有，关闭后释放。长时间不关闭连接的大规模峰值尚未做压力测量，不据此宣称生产资源上限已验证。

## 独立结果

原始证据全部在私有忽略目录 `work/external-archive-implementation/`。

| 验证 | 结果 | 证据 |
|---|---|---|
| 初始Linux独立用例 | FAIL：7项均在正常WAL初始化处报错 | binding-independent-red.log |
| 修复指针后中间复验 | 10通过 / 2失败：锁保持与注册回退 | binding-interim-vfs.log |
| 修复守卫后中间复验 | 11通过 / 1失败：注册回退 | binding-interim-guard.log |
| Codex最终独立Linux行为集 | **13/13 PASS** | binding-independent-round1.log；exit0 |
| Codex+GLM合并Linux测试（strace下实际运行） | **19通过 / 1失败** | binding-linux-final.log；exit1；20项 |
| macOS完整Python回归 | 73通过 / 1失败 / 20跳过 / 65 subtests通过 | binding-pytest.log；失败为父任务已有C0，不是本子任务新回归；20个Linux测试在Mac跳过，不算通过 |
| `cargo test --workspace` | PASS：842通过 / 48 suites / 11 ignored | binding-final-cargo-test.log；exit0 |
| `cargo clippy --workspace --all-targets -- -D warnings` | PASS | binding-final-clippy.log；exit0 |
| `cargo build --release` | PASS（仅构建） | binding-final-release.log；exit0 |
| `scripts/check-no-secrets.sh` | 首次FAIL；修正文档后PASS（exit0） | binding-final-secrets.log、binding-final-secrets-docs.log、binding-secrets-recheck.log；报告中的完整SHA-256被识别为可疑值，并非实际凭据；改为引用私有清单，扫描规则未改变 |

13项独立行为包括：正常提交/回滚/重开/integrity、目录ABA+无关fd、打开瞬间主文件symlink、WAL/shm/journal侧文件symlink不修改外部对象、根丢失拒绝下一事务、100次开关fd不泄漏、SIGKILL后未提交内容不存在、WAL长读一致性与checkpoint、两进程writer互斥、原生POSIX锁保持、注册失败拒绝、12MB temp spill、未关闭cursor/BLOB下关闭连接安全。

strace原件 `binding-linux-final.strace`（仅操作元数据，合成目录）；摘录 `binding-trace-excerpt.log`。捕获514条固定fd路径相关系统调用，作为行为证据，不把database_list的路径字符串自报当作唯一证明。

## 尚需处理

当前唯一Linux测试失败：GLM自有 `test_symlinked_intermediate_dir_rejected`。Linux以 `O_DIRECTORY|O_NOFOLLOW` 打开中间符号链接时返回 ENOTDIR，代码归类为 `path_open_failed`；测试只接受 `path_symlink_rejected` 或 `cross_device_rejected`。**实际已拒绝，未跟随到目标；但消费者可见错误码与测试不一致，不能把失败标成通过。** 由原GLM实施者修正错误分类，或有明确语义依据地修正其自有测试并保留拒绝行为证明；Codex不代改、不删门。

临时名称在VFS/连接关闭时释放；长连接反复spill的峰值与关闭策略在最终复审仍须说明。真实卷身份、挂载变化、真实外置盘锁/fsync/加密/权限与实际NAS仍未验证，不在合成通过中偷换为生产通过。

## 配额与停止

18:08 +08实际GLM返回 `429 / 1308`，服务提示 **2026-10-04 19:57:59** 重置，进程exit1。证据 `binding-glm-round1c.log`。这只是服务提示时间，未验证恢复；不切换模型，不把当前代码称完成。

已保存代码/测试/日志。独立Colima profile `wx-archive-v3-test`停止，停止证据 `binding-vm-stop.log`；原default未启动，未改Docker当前context/用户SSH配置。没有真实微信正文/密钥/NAS读取，未更改调度/MCP/TCC/签名。

恢复步骤：GLM只续本子任务剩余错误码/测试语义及最终自验；Codex重新跑受影响Linux集、回归并绑定新代码指纹。子任务明确通过后才恢复父任务C0、源端完整性、collector/projection/backup/migration/CLI工作，不需再次申请已经批准的代码范围。真实采集/生产切换仍另走原授权门。

文档门补充：上一份续做报告和本报告把完整SHA-256指纹写进非忽略Markdown，实际触发密钥扫描。现改为引用私有JSON，不豁免或改动门禁。之前命令串最后的git diff退出码不能代替密钥扫描退出码；本轮分别留存实际日志与重验状态。
