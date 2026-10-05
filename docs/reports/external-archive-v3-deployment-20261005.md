# v3 代码提交、推送与懒猫部署

2026-10-05，用户明确要求“直接提交、推送、部署”，随后要求继续。本次授权覆盖原先本任务“不提交/不推送”的限制；保留feat/archive-layer，不改main。**代码部署完成，真实采集服务尚未启用，父任务仍review。**

## 已完成

- 代码提交：`c9984532a8ee9b2d928cebf1eeebaf1c9ef906c1`，58个文件。提交前源码与上一轮213文件指纹相同；复用已完成的Rust883、Mac Python247、Linux Python281及clippy/release验收，未将新构建当生产验证。提交钩子及密钥扫描通过。
- 推送：`origin/feat/archive-layer`，远端为`jmuguy/wx-cli`；git ls-remote核对代码提交一致。没有改main、force-push或覆盖远端已有分支历史。
- 部署：从现有lzc配置确认唯一READY默认盒子goldmine，既有严格host-key SSH连接成功，先读取远端`/AGENTS.md`及`/lzcsys/data/README`。没有递归扫描用户数据、容器存储或其他卷。
- 代码版本目录：`/root/.local/share/howie-wechat-archive-v3/releases/c9984532a8ee`；`current`首次发布指向该版本，previous_current=null。源码包SHA256校验通过，266个提交文件逐一SHA256一致。没有把Mac二进制复制到Linux。
- 运行环境：Linux x86_64，Python3.11.2，SQLite3.40.1，FTS5 trigram可用。系统没有ensurepip/cryptography，使用私有venv及本地准备、校验后上传的离线wheels，安装cryptography49.0.0、pytest9.0.2、pip26.2.1及其依赖。没有apt/sudo安装或修改系统Python/系统配置。
- 实机合成验收：`test_codex_v3_cli_gate.py`、`test_codex_stdio_transport_gate.py`、`test_codex_backup_gate.py`、`test_codex_migration_gate.py`、`test_codex_projection_gate.py`在已部署源码/venv中运行，**33 passed，exit0**。`current`入口的CLI help exit0。

私有证据位于`work/external-archive-implementation/`：`publish-commit-20261005.log`、`publish-push-20261005.log`、`publish-remote-verification-20261005.log`、`publish-release-manifest-20261005.json`、`deploy-target-20261005.json`、`deploy-source-verification-20261005.log`、`deploy-isolated-install-20261005.log`、`deploy-nas-synthetic-acceptance-20261005.log`、`deploy-publish-current-20261005.log`。服务器版本目录内有`RELEASE.json`及源码校验清单。部署包不含真实数据、密钥或本地work日志。

## 尚未启用与后续顺序

此次证明既有SSH可达、代码与依赖实际安装、目标机上的合成业务链可运行；没有证明外置盘生产卷身份、受限forced-command SSH、独立查询用户或真实数据链路。`/root/...`代码目录仅供当前管理员部署验证，不能直接当独立查询OS用户的最终runtime权限布局。

1. **补齐生产恢复能力**：当前restore-drill仍仅显式synthetic，生产路径默认拒绝；完成production卷绑定及新epoch恢复的代码和验收。
2. **验证真实部署条件**：确定外置盘archive root、UUID/挂载、加密、锁/fsync/O_TMPFILE、容量、writer/query账户与可读runtime位置；受限SSH两角色独立；真实备份恢复验证。数据目录不能落在本次代码版本目录。
3. **受控真实样本**：取得明确真实范围/源开销授权，先选已批准的小样本，核对源身份、媒体缺口、微信UI及问答；Claude独立评审与实际模型工具调用分别记录。
4. **全量与切换**：先盘点全部获准会话和空间/耗时，按清单分批与断点采集，完整对账；保留v2与回滚，明确批准后短暂停旧writer、迁移、切换与实际整点触发验收。

没有初始化真实v3主库、扩大采集/AI白名单、创建或启动采集服务、修改现有Mac v2调度/MCP/TCC/签名、删除原归档。撤回本次代码部署可停止使用current入口；没有后台新服务需要停止，版本目录可保留用于回查。用户的部署授权已执行，后续不应重复询问同一次代码部署许可；真实采集范围和业务切换须明确后再执行。
