# Linux 固定卷绑定独立验收通过

2026-10-04。子任务 TASK-20261004-174427-7D96：**PASS，限定代码及隔离 Linux 合成环境**。父任务 TASK-20261004-075803-13F5 可恢复实施，尚未完成。GLM-5.3 实施，Codex 独立审查和测试；无提交、push 或生产改动。

## 修复与验证

额度恢复后原 GLM 会话实际响应。此次进一步发现并修复临时文件同名冲突后 SQLite 只读重试导致删除冲突文件、ctypes io_methods ABI 字段顺序错误、SQLite 数值常量错误导致静默丢行。临时文件现在通过持有的卷目录 fd 使用 Linux O_TMPFILE；不支持时拒绝，不退回 /tmp。仅匿名私有临时文件使用无共享锁实现；主库/WAL/shm 保留 SQLite unix VFS 锁。

证据均位于私有忽略目录 `work/external-archive-implementation/`：

| 门禁 | 实际结果 | 证据 |
|---|---|---|
| Codex Linux 独立行为 | 18/18 PASS，exit 0 | binding-retry-anon-retest.log |
| GLM 与 Codex 合并 Linux | 34/34 PASS，exit 0 | binding-retry-anon-merged.log |
| 临时文件真实系统调用与数据 | PASS：O_TMPFILE、nlink=0、同卷、12MB 内容读写 | binding-retry-anon-boundary-green.log 与 .strace |
| macOS 完整 Python | 73 passed、1 failed、34 skipped、65 subtests passed；exit 1 | binding-retry-pytest.log |
| Rust workspace / clippy / release | 842 passed、11 ignored；clippy/release PASS，复用同源码结果 | binding-final-cargo-test.log、binding-final-clippy.log、binding-final-release.log |
| Rust 未变化核对 | changed_rust_files=[] | binding-retry-rust-unchanged.json |
| 密钥扫描 | PASS，exit 0 | binding-retry-secrets.log；文档后再次核对另存 binding-accepted-secrets.log |

Python 唯一失败是父任务已知 C0 伪造覆盖缺陷，保留红测，不能把完整 Python 门称为通过。Linux 用例在 Mac 跳过不算通过。最终代码和测试指纹保存在 `binding-accepted-code.json`。

独立行为还覆盖正常提交/回滚/重开/integrity、根目录 ABA 与无关 fd、主文件及 sidecar symlink、根丢失、并发锁与 checkpoint、SIGKILL 恢复、连接及长连接 spill 资源释放、活跃 cursor/BLOB 关闭、部分 I/O、磁盘满与不支持匿名文件时拒绝回退。旧失败日志保留；旧 named-temp 注入点已被匿名文件设计替代，放行依据是新的实际 inode 和内容证明。

## 未验证及后续

真实设备身份、挂载/加密/权限、目标文件系统 O_TMPFILE 支持、真实外置盘 fsync/锁、源读取开销与真实群人工核对均未验证。本验收不授权真实扩大采集或生产切换。隔离 VM 可继续供父任务合成测试，原 default 环境不动。

父任务继续由 GLM 完成 C0、源端完整性及 collector/projection/backup/migration/CLI，Codex 逐项独立验收。历史报告 `linux-volume-binding-20261004.md` 保留失败过程，以本报告为当前子任务结论。
