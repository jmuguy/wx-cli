# Phase 4 — 只读 MCP 与客户端

日期：2026-10-03。任务：TASK-20261001-173002-8057。本报告只保存运行状态、计数和引用 ID，不保存真实聊天正文或客户端凭证。

## 已完成

用户批准用户级注册。Claude Code 2.1.287 已注册 stdio `wechat-archive`：

```bash
claude mcp add --scope user --transport stdio wechat-archive -- \
  /opt/homebrew/bin/python3 \
  /Users/jmuguy/Documents/Coding/wx-archive/archive/wechat_archive.py \
  --root '/Users/jmuguy/Library/Application Support/howie-wechat-archive' mcp
```

修改前完整用户配置私有备份在 setup-backup-20261002-200929 中，备份 0600、父目录 0700。原有三个 MCP 配置前后比较不变；不把其他服务配置或任何凭证写入仓库。曾注册的 Cellar patch-version 解释器路径已移除，当前用稳定 Homebrew 入口，不留下重复别名。未擅自修改 Codex 客户端配置。

公开且实际返回三个工具：`search`、`get_context`、`list_conversations`。账号由私有配置固定；会话唯一白名单 `45416093717@chatroom`。工具不提供写入、采集、密钥读取或账号覆盖。所有工具标记 readOnly，消息正文作为不可信数据而非执行指令。

MCP 以 SQLite mode=ro/query_only 打开已有库，不创建、不迁移、不 chmod；无库或无配置直接失败。server_id 大于 2^53-1 必须传十进制字符串，数字分支拒绝不安全值；字符串不能超出 signed 64-bit。local-only 使用完整 message_id 与 id_kind=local。无效参数返回 isError，后续请求仍能正常检索。

## 实际 stdio 消费证据

从已持久化的 Claude 用户配置中取出本服务启动命令，实际启动进程并依次执行 initialize、tools/list、三个工具调用，exit 0。不是只检查 JSON 配置存在。

- 首次真实引用 SID `5952263889272536570`，local_id 2820，2026-10-02 00:22:07 +08；原话/发送人与原始来源对齐，内容不进报告。
- 全历史后再次三工具实跑；search 与 get_context 都包含 SID `3920274865756710861`，context 4 条。5131 个既有文件的尺寸、mtime、权限与归档数据库字节散列调用前后完全一致，没有新增文件。
- 对真实约定/历史待办/决策背景，按用户许可自选题目并使用 search/get_context 取证；引用 ID、结构及未确认项记 phase-5。
- 62 个 Python 行为回归 + 65 subtests 通过；含跨账号/会话拒绝、无写工具、缺配置不创建库、越界请求后继续查询、大 ID 字符串精度边界。不是使用消息正文充当可执行提示词。

上述是实际注册命令的 stdio 协议运行与消费，不等于 Claude 模型主动调用工具。

## 额度恢复后的真实 Claude 模型调用

2026-10-03 用户确认额度恢复后，重新运行 claude-sonnet-4-6。使用 restricted、strict-mcp-config、无内建工具，仅显式加载既有注册的本服务；禁用会话持久化，原始 stream-json 留在仓外 0600 文件。

- 第一次模型真实调用 `mcp__wechat-archive__search` 成功，命中 SID/message_id `3920274865756710861`。这已满足“真实 Claude Code 模型主动调用一次 search”，不是 Python 模拟协议。
- 同次 get_context 的模型参数被传为舍入后的大 JSON 数字，服务明确拒绝，未返回错误引用。没有把此失败标为上下文成功。
- 只做一次回退，使用现有 `message_id` 字符串 + `id_kind=server`：Claude 模型实际 search/get_context 两次 tool_use 与对应成功 tool_result 均可核对，两个结果的 message_id 完全一致。进程 exit 0、stderr 0，归档数据库字节与 mtime 在回退调用期间不变。
- 建议 Claude 调用上下文优先使用字符串 message_id；server_id 数字精度守卫继续保留，不因模型错误而放宽。真实 UI 和用户答案判断仍不由此代替。


## 独立 Claude 评审与修复复核通过

曾实际执行 Claude Code print 模式，模型 claude-sonnet-4-6，safe-mode、strict-mcp-config、tools 空、no-session-persistence，输入为不含真实聊天和密钥的源码评审材料。返回 API 429，提示会话额度到限、11:40pm（Asia/Manila）重置；输入/输出 token 均为 0。未为了确认限额反复重跑。

只读 reviewer 静态检查发现一个 P1：预期本地缺失的 os error 2/文件名包含 error 被当成硬错误。父会话先用行为夹具复现，再修复为按真实行首/ANSI 日志 severity 判断；保留未知警告硬失败。此检查不是 Claude，不能替代计划要求的 Claude 独立验收。

额度恢复后针对 e734f5b 的最终实现执行独立评审：13 个完整源码文件输入在 900 秒超时，没有结论；只做一次回退，输入全部 19 个生产文件差异，没有缩减生产代码范围。claude-sonnet-4-6 进程 exit 0、stderr 0，verdict=fail，唯一 medium finding 为 `KeyStore::save` 固定 `keys.toml.tmp` 遗留后永久报 AlreadyExists。其余主要覆盖项通过，另有 rowid 可选列和旧 MediaBridge 流式路径两个上下文未验证项。

该 finding 经真实文件系统回归先复现失败（artifact 258）。修复将 tempfile 从 dev 依赖移入正常依赖，使用同目录唯一 NamedTempFile、write_all 后 persist 原子替换；写入/替换失败只清理本次临时文件，不删除或覆盖未知旧文件。保留回归 `interrupted_sibling_does_not_block_or_get_overwritten_by_later_save`，确认实际值重载与旧文件原样保留。

实际进程 smoke 使用隔离目录、仅合成密钥，输出 `PASS: interrupted sibling preserved; later save reloads; file 0600, directory 0700`。随后完整 Rust 门禁 807 passed/46 suites/11 ignored、clippy -D warnings、release build 均通过（artifact 261）。没有写真实 keystore、重新提取密钥或重签微信；throwaway example 与合成目录已清理。

最终只读修复复核使用相同 Claude 模型、effort=medium、禁工具/会话持久化，提供 save 完整关键路径、依赖变更和原始上下文：SELECT 为 8 个基础列、条件追加两列、最后追加 rowid；归档 export 调用 resolve_parallel/collect，图片转换失败为 hard_failed，不经过旧 MediaBridge 的流式软失败路径。Claude exit 0、is_error=false，verdict=pass、findings=[]、unverified=[]，唯一缺陷 resolved、rowid resolved、媒体路径 resolved_as_described（父会话调用链证据佐证）；不是把 smoke 当静态评审。私有输入/结果位于 ROOT/claude-acceptance-pf6ahtan，文件 0600、目录 0700。

因此独立 Claude 条件与真实模型 search 条件均通过。实际用户 UI 与三个答案的业务判断仍待 phase-5，不冒充整任务完成。
