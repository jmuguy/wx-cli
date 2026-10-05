# Linux 固定卷绑定独立实施任务

用户2026-10-04明确批准从父任务TASK-20261004-075803-13F5拆出，GLM-5.3实现，Codex独立验收通过才继续父任务。本轮重新限定范围，最多3轮返修。

目标：Linux本地SQLite master及journal/WAL/shm/临时文件固定到已验证外置盘目录，不被路径替换、ABA、无关fd、sidecar symlink带到替代位置；正常事务/重开/并发读/锁/崩溃恢复必须真实可用。错误fail closed、不得预先写错库。

允许：archive/v3/vfs.py、guard.py、master.py仅连接/生命周期相关、errors.py、新增底层绑定模块和必要依赖声明、自有Linux测试。不得修C0或其他collector等模块，不改Rust、生产、真实源/密钥/NAS/调度/权限、不commit/push、不改Codex独立测试。Codex管理docs和任务池。

可选择成熟APSW/native小shim等依赖或修现有方案，但要证明同一SQLite库ABI与文件名/URI元数据生命周期正确，不能手工自造锁。现有ctypes xOpen c_char_p转换临时bytes会丢SQLite filename关联元数据/生命周期，是WAL失败重点。应审阅本机cargo registry sqlite3.c/h及Linux安装文档源码。避免不可靠fd扫描和写探针。不受信任/不支持平台生产拒绝；Linux合成必须走真实生产相同绑定路径，不允许fallback骗过测试。

验收：
1 Linux实际初始化、WAL事务提交/回滚、关闭重开、integrity_check，日志exit0。
2 目录swap/ABA+无关fd、主文件/sidecar symlink及跨设备指向，替代库字节不变。主卷丢失/身份变化/epoch更新在下一操作拒绝，不能内置盘fallback。
3 两进程写锁互斥，长读+新写+checkpoint及SIGKILL后重开完整、未提交行不存在。
4 大量连接开关无fd/回调泄漏，temp spill不得默认/tmp内置盘；可显式禁用未支持操作并有容量有界的可用正常链路，不以禁用所有写冒充安全。
5 ctypes生命周期/错误路径/不支持平台fail closed；只以代码string或database_list不足证明通过。
6 原Codex Linux复现脚本和针对本任务独立行为测试通过；Mac原测试回归不得新失败（已知C0红测保留，与本任务分开）。完整Python/Rust/release/secrets门最终分别记状态。

隔离环境：已存在colima wx-archive-v3-test（无挂载/未改default context），主控启动。只用 `colima -p wx-archive-v3-test ssh -- ...`。/tmp/wx-v3-review只是合成环境。禁止触碰default或真实NAS。可在隔离VM装测试依赖、不用宿主sudo。不要读取任何实际凭据。

历史证据docs/reports/external-archive-v3-retry-20261004.md；work/external-archive-implementation/resume2-linux-binding-probe.py和retry-final-linux-binding.log。实施日志放同work目录 binding-*；本任务完成后输出实际命令结果、改动清单、剩余限制并停止，由主控独立验收；不要自行扩展回父任务。


独立测试入口：`PYTHONPATH=. python3 archive/tests/test_codex_linux_binding.py`（实际Linux），Mac仅skip不算通过。主控已确认初始错误扩展码1032 / SQLITE_READONLY_DBMOVED；另有POSIX锁复验与Linux注册失败禁止fallback门。

实施中补充：源码声明支持temp spill，因此要求FILE temp_store下12个1MB blob实际写入/读回且临时文件留在固定目录，不能以回调TypeError被吞后CANTOPEN算功能通过。名字缓存须随VFS/连接生命周期释放，不得持久进程无界累积。

21:59返修补充：独立strace证明base unixOpen在O_EXCL失败后会只读重试并删除冲突文件，旧槽位名方案不放行。改O_TMPFILE后旧binding-retry-temp-race.py的stat注入点不再经过，不能单凭未触发声称通过；Codex新版binding-retry-temp-boundary.py同时接受冲突文件保全，或F_GETFL证实O_TMPFILE+st_nlink=0+同卷及12MB数据实际成功。正式Codex测试也已加入匿名inode证据和EOPNOTSUPP拒绝回退，勿修改Codex测试。
