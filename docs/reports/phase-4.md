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

## 未通过的独立 Claude 门禁

曾实际执行 Claude Code print 模式，模型 claude-sonnet-4-6，safe-mode、strict-mcp-config、tools 空、no-session-persistence，输入为不含真实聊天和密钥的源码评审材料。返回 API 429，提示会话额度到限、11:40pm（Asia/Manila）重置；输入/输出 token 均为 0。未为了确认限额反复重跑。

只读 reviewer 静态检查发现一个 P1：预期本地缺失的 os error 2/文件名包含 error 被当成硬错误。父会话先用行为夹具复现，再修复为按真实行首/ANSI 日志 severity 判断；保留未知警告硬失败。此检查不是 Claude，不能替代计划要求的 Claude 独立验收。

尚未完成：额度恢复后用最终提交独立评审，并在真实 Claude Code 会话主动调用一次 search；用户自行使用已注册服务也可提供实际调用证据。客户端可连接、Python stdio 调用、reviewer 的结论均不冒充这两个条件。任务此门禁仍 blocked。
