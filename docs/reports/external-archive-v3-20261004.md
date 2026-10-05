# 外置盘 v3 独立实施验收（2026-10-04）

**历史报告（14:55）：FAIL / 退回修改，当时阻塞于 GLM 配额。额度现已恢复，后续结果见 [续做复验报告](external-archive-v3-retry-20261004.md)；仍未通过，禁止切换。**

任务 TASK-20261004-075803-13F5；分支 feat/archive-layer；基线 HEAD `601b34ce9ea30bd19d913e903a1213e96d7a5dd5`，所有本轮改动未提交。已审代码工作区指纹 SHA-1 `d54cb6a3d6fa00c9161008083efd44e40c76becd`（文件清单与算法输入见仓外于Git的 `work/external-archive-implementation/reviewed-code.json`）。之后代码变化需重新验收。

## 执行与授权

已读用户指定实施交接、任务池完整原 note、AGENTS、现有交接、PLAYBOOK及设计round-5/round-4证据。任务由backlog按CLI转ready再claim；以aitask维护，不直接编辑任务池。用户已授权代码、合成验证和非破坏性预检，设计无需重复批准；生产门保持。

实际调用本机 glm-run（BigModel Anthropic兼容端点），模型参数和响应均为 `glm-5.3`；继承MCP关闭。Rust源端会话 `8215ca10-880d-4083-bd1d-8f88d0747fff`，NAS侧会话 `be7e6bde-c777-4770-a82d-8c7bfbe8acf5`。Codex负责独立测试与审查，未替实施者修复生产代码；新增两份独立测试文件，维护报告与交接。

两会话在14:53 +08均返回 `429 / 1308`：已达到5小时使用上限，服务提示 `2026-10-04 17:56:36` 重置。两个进程已退出1，没有后台实施继续。日志 `glm-source-output.log`、`glm-nas-review1-output.log`；不把预计重置时间当恢复成功。

未读取真实微信正文/密钥、未连接NAS、未修改TCC/签名/生产配置/调度/MCP，未push/合并。只读FileVault探针返回 `FileVault is On.`；不代表外置盘加密通过。release门已构建仓内target/release/wx-cli，未覆盖用户~/.local/bin/wx；现有源码命令默认路径无改动，新入口未接入调度。

## 独立验收命令及实际结果

所有原始日志在本仓私有工作目录 `work/external-archive-implementation/`（精确git/info/exclude忽略；不入提交）。

| 命令 | 状态 | 实际证据 |
|---|---|---|
| `cargo test --workspace` | FAIL | exit101；新增archive_snapshot单测三处`&str + &String`编译错误；final-cargo-test.log |
| `cargo clippy --workspace --all-targets -- -D warnings` | FAIL | exit101；上述编译错误及unused_mut/needless_borrow；final-clippy.log |
| `python3 -m pytest -q archive/tests` | FAIL | exit1；66 passed、2 failed、65 subtests passed；final-pytest.log |
| `cargo build --release` | PASS（仅构建） | exit0；final-release.log；不代表行为/部署通过 |
| `scripts/check-no-secrets.sh` | PASS（工具覆盖范围） | exit0；final-secrets.log；不覆盖Git历史、忽略文件和仓外日志 |
| `cargo test -p wx-db --test codex_snapshot_gate` | PASS | 2 passed；codex-snapshot-retest3.log |
| `cargo test -p wx-cli --test archive_snapshot_cli` | FAIL | 7 passed / 3 failed；final-snapshot-cli.log |
| `git diff --check` | PASS | exit0；仅已跟踪diff格式 |

基线独立实测为Rust807 passed / 46 suites / 11 ignored、Python63 passed + 65 subtests，保留baseline日志；不能拿基线代替最终门禁。

## 必须修复的问题

1. **P1：采集授权隐式放开AI查询。** `archive/v3/master.py` initialize使用`grants.get('query', 1)`；只给capture授权的私聊能通过query权限检查。独立回归 `test_capture_grant_does_not_implicitly_grant_ai_access` 实际失败。缺查询策略必须拒绝，接收端仍须独立检查。
2. **P1：主库未真正绑定已验证目录。** 同文件连接前prove_db只检查路径，随后SQLite按绝对路径打开；在两者之间替换目录，可成功读取替代主库。独立回归 `test_master_open_cannot_follow_replaced_archive_path` 实际读到replacement而非original。`mode=rw`和持有额外fd均不消除此竞态；生产必须固定目标卷与对象，无法证明则拒绝。
3. **P1：快照输出可进入源目录。** `output_inside_source_is_rejected` 实测命令返回成功，违反禁止写源边界。检查源码显示source使用canonical路径、out使用词法absolute，macOS `/var`与`/private/var`别名造成比较不一致；须统一真实祖先解析并保留符号链接/竞态防护，修复后确认源树无新增文件。
4. **P1：整体实现不完整。** `archive-inspect`、全部库的完整会话导出/媒体连接、SSH上传协议与队列、查询投影/隔离、独立加密备份与日志恢复、v2一致迁移、CLI与端到端接合均尚未实现，不能归因于仅缺设备验证。NAS仅有errors/util/guard/master/reconcile基础模块。
5. **P2：Rust完整门禁失败。** `archive_snapshot.rs`单测三处字符串拼接编译错误，clippy警告仍未清零；本轮不以修改测试门槛代替修复。
6. **未闭环：watchdog严格时限。** 当前事件轮询失败使用unwrap_or_default，Pinning事件收到后才计时，另有100ms slack；没有父进程确认握手来证明整个持锁窗口受批准T_max约束。集成`parent_kill_releases_source_locks`通过只证明该合成用例，不能覆盖事件丢失/阻塞与真实源时限。

另两个快照CLI失败分别为包含源目录的错误文案不符、重复代次测试未得到预期JSON。需区分测试传参/错误文案与实现缺陷，不能把失败数量直接等同于缺陷数量。

## 已修复且独立复测的局部行为

- 快照发布曾保留group/other读位且覆盖旧代次；Codex两项红测均已转绿，日志保留。
- 目录fsync错误曾被吞、short write截断、mountinfo含空格路径解析错误；Codex三项红测已转绿，完整pytest仍有上列两项失败。
- GLM新增的源并发写固定view、derived-only、非默认page等7项快照核心测试曾由Codex实际运行通过；S1后续完整自测日志是实施者证据，不替代最终workspace门禁。
- 新快照CLI定向测试7项通过（含部分库失败不发布完整manifest、父进程kill后源锁释放等），其余3项失败，不称S2完成。

## 未验证与续做顺序

所有真实设备/真实源/源读锁与shm/WAL开销/T_max/OS隔离/受限SSH/实际定时/UI/恢复门仍UNVERIFIED。范围类别、最大延迟、RPO、对账阈值、备份故障域尚未获实施前批准；外置盘访问路径与身份尚不明确。不得为此降级到内置盘、旧cache或SQL-only。

1. GLM额度恢复后沿原会话续做，先读本报告及独立红测，不重新读真实密钥或扩采集。
2. 修复P1边界与Rust门禁，再完成S3/S4。跨语言契约草稿在work/nas-contract.md（该位置相对证据目录），待实现接合验证后迁入正式文档；当前不是可用接口。
3. 追加迟到/编辑/rowid复用/媒体升级、ACK丢失/竞争、投影侧信道/撤权、C0原子性、独立加密恢复等行为测试并执行完整门禁。
4. Codex再次独立验收；通过代码与合成门后，准备可审阅的真实样本范围、磁盘身份/加密权限、开销实测计划和恢复方案，再请求生产相关授权。本轮不请求生产批准。

技术依据：[SQLite backup](https://sqlite.org/backup.html)、[SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)；文档语义不代替fixture与真实格式验证。
