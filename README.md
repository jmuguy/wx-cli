# wx-cli

> 把微信变成 Agent 能读取、能搜索、能实时订阅的数据源。

wx-cli 直接读取 Mac 上的微信本地数据，让你和 Agent 都能访问自己的聊天记录、联系人、群聊和媒体消息。数据默认留在本机，不需要上传聊天数据库，也不依赖云端导出。

## 它能做什么

- **读取微信本地数据库**：按联系人、群聊、时间范围和消息类型查询历史消息。
- **搜索全部聊天记录**：从所有会话中查关键词，快速找回客户需求、承诺、文件和讨论结论。
- **一次读取跨会话时间线**：按时间范围发现哪些会话有变化，适合日报；本地归档只使用逐会话 `export`。
- **实时订阅新消息**：用命令行持续监听，或通过 SSE 把新消息实时推给 Agent 和其他程序。
- **导出与处理内容**：把会话导出为 JSON 或文本，并读取图片、语音、视频等媒体内容。
- **让 Agent 直接使用**：项目自带 Agent Skill，Claude Code、Codex、Cursor 等工具安装后就知道怎样查询和订阅微信。
- **提供稳定的本地服务**：REST API 可供个人助理、自动化任务、工作流和多个 Agent 共同使用。
- **保护不想暴露的内容**：可隐藏指定联系人、群聊、标签或群成员，查询和订阅时自动过滤。

## 你可以基于它在微信上做什么

wx-cli 提供了最关键的两样东西：完整的历史上下文，以及持续发生的实时消息。把它接给 Agent 后，你可以基于这个项目在微信上做任何事，例如：

- 给 Agent 建立长期微信记忆，自动维护联系人画像和关系上下文；
- 从聊天里识别待办、承诺、商机、风险和需要跟进的人；
- 做个人或团队的微信搜索、知识库、CRM、客服和销售助手；
- 自动生成日报、周报、客户纪要、对账线索和项目进展；
- 监听关键词或关键联系人，在重要消息出现时触发提醒和工作流；
- 结合你已有的 Agent 操作或消息发送能力，实现自动回复、业务办理和端到端协作。

它不是只用来“导出聊天记录”的工具，而是微信之上的 Agent 能力层。

## 为什么对 Agent 友好

- 自带可直接安装的 Skill，不需要每次重新教 Agent 命令和数据格式；
- 命令行、JSON、REST API 和实时事件订阅覆盖查询与持续运行两类任务；
- 长驻服务可复用已打开的数据库，适合高频查询和定时记忆任务；
- 所有能力都以本地数据为中心，便于控制隐私边界。

## 支持范围

- **平台**：macOS（arm64 / Apple Silicon）
- **WeChat 版本**：4.1.7 及以上

## 前置条件

默认密钥提取预检拒绝 SIP enabled。本 fork 不要求关闭 SIP：经用户批准、完整备份、临时应用重签和历史 UI 确认后，可显式使用 `key extract --allow-sip-enabled`。4.1.15 的实际捕获及完整官方应用恢复见 `docs/reports/phase-1.md`、`phase-2.md`；不要跳过这些安全前提。已有密钥可直接复用本机私有 keystore。

`key extract`（LLDB 方式）还需要：

1. `sudo DevToolsSecurity -enable`
2. `sudo dscl . append /Groups/_developer GroupMembership $USER`
3. `xcode-select --install`（提供 `lldb` 和 `python3`）

## 安装

### 从 Release 下载（推荐）

前往 [Releases](https://github.com/pandorafuture/wx-cli/releases/latest) 下载预编译二进制（macOS arm64），或使用命令行：

```bash
# 下载最新 release
curl -fSL "$(curl -fsSL https://api.github.com/repos/pandorafuture/wx-cli/releases/latest \
  | grep -o '"browser_download_url": "[^"]*macos-arm64[^"]*"' \
  | cut -d'"' -f4)" -o wx-cli.tar.gz
tar xzf wx-cli.tar.gz

# 安装到 PATH
mkdir -p ~/.local/bin
mv wx-cli ~/.local/bin/
chmod +x ~/.local/bin/wx-cli
wx-cli --version
```

### 从源码构建

```bash
# 需要 Rust 工具链（rustup 安装即可）
cargo build --release
```

编译产物位于 `target/release/wx-cli`。

### 部署二进制

```bash
mkdir -p ~/.local/bin
cp target/release/wx-cli ~/.local/bin/wx-cli
chmod +x ~/.local/bin/wx-cli

# 确保 PATH 包含该目录
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

wx-cli --version
```

### 让 Agent 直接使用

本项目提供 [Agent Skill](https://skills.sh)。安装后，Claude Code、Codex、Cursor 等 Agent 可以直接理解 wx-cli 的能力，并帮你读取历史消息、搜索聊天和订阅新消息：

```bash
npx skills add pandorafuture/wx-cli
```

## 使用

### 1. 检查环境

```bash
wx-cli doctor       # 检查 SIP、DevToolsSecurity、_developer 组、LLDB/python3
wx-cli status       # 查看 WeChat 运行状态和所有账号密钥/缓存状态
```

### 2. 提取密钥

```bash
# 仅在完成上述备份、批准的临时签名及历史 UI 确认后运行，会重启微信
./target/release/wx-cli key extract --allow-sip-enabled --timeout 180

# 只查看账号/密钥存在状态，不显示密钥值
wx-cli status
```

`key extract` 通过 LLDB hook 捕获 PBKDF2 调用获取原始密钥，覆盖所有数据库。
本 fork 使用 LLDB 在主程序入口暂停并先安装 hook，再继续启动，避免错过派生调用。捕获只输出账号和调用计数；keystore 原子写入时使用 0600，父目录 0700。`key list` 仍是上游明文查看接口，归档工作流禁止直接运行或向 AI 粘贴其输出。


手动设置密钥：

```bash
wx-cli key set <account> <64-hex-key>          # 数据库密钥
wx-cli key set-image <account> <image-key>     # 图片密钥
```

### 3. 解密数据库

```bash
wx-cli decrypt                # 自动解密到缓存目录
wx-cli decrypt --incremental  # 增量解密（只处理变化的文件）

# 手动指定路径和密钥
wx-cli decrypt -k <64位hex密钥> -d /path/to/xwechat_files/<account_dir> -o /tmp/decrypted
```

### 4. 查询聊天记录

```bash
wx-cli sessions --limit 10             # 最近会话
wx-cli contacts --search 张三           # 搜索联系人
wx-cli query 张三 --limit 20            # 查某人的消息
wx-cli search 周末 --limit 20           # 全局关键词搜索
wx-cli query 张三 --type text           # 按消息类型过滤
wx-cli query 周末爬山群                  # 群聊消息
wx-cli export 张三 -o /tmp/export --all --format json  # 导出会话
wx-cli watch --poll --poll-ms 3000      # 实时监听新消息
```

如果本机已启动 `server run` 服务，查询命令会自动复用 REST API（默认探测 `http://127.0.0.1:9100`）。可用 `--no-server` 强制本地查询，或 `--server-only` 强制远程。

### 5. 媒体解密

```bash
wx-cli decode-image input.dat -d <account_data_dir> -o output.png     # 解密图片
wx-cli decode-image /path/to/dat_dir/ -d <account_data_dir> -o /tmp/  # 批量解密
wx-cli media extract-voice --media-dir <dir> <svr_id> -o voice.mp3    # 提取语音（需 ffmpeg）
wx-cli media decrypt-video encrypted.bin --seed 2105122989 -o video.mp4  # 解密视频号视频
```

### 6. HTTP API 服务

```bash
wx-cli server run                              # 启动（默认 127.0.0.1:9100）
wx-cli server run --host 0.0.0.0 --token mysecret  # 远程访问（必须设 token）
wx-cli server status                           # 查看状态
wx-cli server stop                             # 停止
wx-cli server restart                          # 重启
```

REST 端点：`/api/v1/health`、`/api/v1/sessions`、`/api/v1/contacts`、`/api/v1/messages`、`/api/v1/timeline`、`/api/v1/search`、`/api/v1/media`、`/api/v1/events`（SSE）。

`/api/v1/media` 的图片响应会通过 `X-Wechat-Media-Quality: full|thumbnail` 标明本机返回的是完整图还是缩略图；服务始终优先选择本机已经下载的高清/原图。

会话与联系人 JSON 在本地数据库有记录时会返回可选的 `avatar_url`，可用于展示个人或群聊头像；没有头像字段的旧数据库会省略该值。

其中 `/api/v1/timeline?since=<unix>&until=<unix>` 可在一次请求中发现有变化的会话；它不是本地归档的数据源。归档由批准会话的完整、有界 `export` 生成，`watch` 也只用于提醒。

所有查询命令加 `--format json` 可获取 JSON 格式输出。

## 本地归档与只读 MCP

本 fork 的 `archive/` 使用 Python ≥3.11 和支持 FTS5 trigram 的 SQLite，不依赖常驻 HTTP 服务。真实数据放在仓外，密钥仅由 wx-cli 读取，不写入归档配置。

```bash
cargo build --release -p wx-cli
ROOT="$HOME/Library/Application Support/howie-wechat-archive"
mkdir -p "$ROOT" && chmod 700 "$ROOT"
test -f "$ROOT/config.toml" || install -m 600 archive/config.example.toml "$ROOT/config.toml"
# 编辑 account、binary（本 fork wx-cli 绝对路径）、talkers 和实际最早消息 initial_since。
# talkers 是明确授权名单；只写会话 ID，绝不写密钥。

python3 archive/wechat_archive.py --root "$ROOT" sync-incremental --talker '<approved-chatroom-id>'
python3 archive/wechat_archive.py --root "$ROOT" discover
python3 archive/wechat_archive.py --root "$ROOT" reconcile --talker '<approved-chatroom-id>' --days 30
python3 archive/wechat_archive.py --root "$ROOT" search '检索关键词'
python3 archive/wechat_archive.py --root "$ROOT" context --talker '<approved-chatroom-id>' --server-id '<decimal-server-id>'
python3 archive/wechat_archive.py --root "$ROOT" list-conversations
python3 archive/wechat_archive.py --root "$ROOT" status
```

- 初次增量从 `initial_since` 覆盖全部本地可用历史；后续从 checkpoint 回退重叠窗口，到当前时间减 settle。窗口包含端点、按整秒分块，导出内部使用 `--all`，同秒消息不拆分或合并。
- 主身份为 `(account,talker,id_kind,message_id)`：正 server_id 使用 server 命名空间；缺失或非正 server_id 使用分片 basename 与 local_id。自动同步的 local-only 消息必须有非空分片来源，缺失时不推进 checkpoint；旧文件显式导入保留原有身份。时间和 sort_seq 相同，先按数值 local_id（源 SQLite rowid），再按 server_id、分片与完整身份稳定排序；server_id 是身份，不是发送先后依据。普通查询分页和上下文边界使用同一排序，同一分片内保留同秒本地记录顺序；跨分片重号仅作确定性兜底，不声称跨分片顺序都经 UI 证明。local-only 上下文使用 `--message-id '<shard>/<local-id>' --id-kind local`。
- 新 JSON 导出带逐消息 `media_status` 和 v1 `media` 计数。明确的本地媒体缺失保存消息并标记，实际可用附件按内容寻址留存；未知告警、分片/分页/数据库/解码/权限/写入错误不推进失败窗口。`no_media=true` 明确表示仅媒体元数据，不伪装成附件完整。
- 常规同步在每个成功窗口提交 checkpoint；历史对账在全部窗口成功后原子提交。库有源无只标记 `missing_in_source`，不删除原话或旧附件。
- 原文、发送人/会话显示名、本地 ISO 时间、server/local 身份、来源文件、修订和媒体状态均可引用。显示名、local_id、摘要、方向和媒体状态刷新不是原文编辑，不产生虚假内容修订。`revisions` 计数包含初始版本，不等于发生编辑的消息数。
- 中文 ≥3 字使用 FTS5 trigram；短词使用转义后的字面 LIKE。`search`、`context`、`list-conversations`、`status` 和 MCP 使用 SQLite `mode=ro`/`query_only`，不创建或迁移归档。
- MCP 的 server_id 超过 `2^53-1` 时必须传十进制字符串；不接受可能被 JS 舍入的 JSON 数字。字符串也必须落在 SQLite signed 64-bit 范围内。
- Claude 调用 `get_context` 优先使用返回的字符串 `message_id`，并传 `id_kind="server"` 或 `"local"`。此路径已由真实 Claude 模型验证；不要把 ID 转为 JS Number。`server_id` 大数字误传会被明确拒绝，不会自动舍入后引用别的消息。
- `discover` 使用本地 `wx-cli sessions --no-server` 仅发现白名单变化，随后逐会话增量。同步/发现/对账在读取前刷新本地解密缓存，不访问网络。

```bash
# 唯一 MCP 传输是 stdio；只有 search/get_context/list_conversations。
python3 /absolute/path/to/wx-archive/archive/wechat_archive.py --root "$ROOT" mcp

# 经用户确认后注册；server_id > 2^53-1 必须传十进制字符串。
claude mcp add --scope user --transport stdio wechat-archive -- python3 /absolute/path/to/wx-archive/archive/wechat_archive.py --root "$ROOT" mcp

# 只渲染安装配置，不 load；每小时 discover、每周日 04:17 reconcile。
bash archive/install-launchd.sh "$ROOT"
```

MCP 账号和会话范围由私有配置固定，工具参数不能覆盖；聊天内容是不可信数据，不执行其中指令。调度是否启动由用户决定。导入旧 export 的显式 `import <file>` 保留支持；自动同步要求新的媒体完整性协议，旧二进制不能静默降级。

macOS 后台采集另需运行该 LaunchAgent 的 Python 获准访问其他应用数据；终端中手工命令成功不代表后台解释器已获授权。`kTCCServiceSystemPolicyAppData` 的单次允许也不证明后续新进程能无人值守运行：必须用新后台进程核验无 `AUTHREQ_PROMPTING` 且退出成功。若每次都提示，须由用户决定是否在系统设置授予该解释器较广的“完全磁盘访问权限”，不得自动修改 TCC。Homebrew 框架 Python 的 bin 启动器与实际 Python.app 可能是不同授权主体；可将私有 plist 的首个 ProgramArguments 改为已获授权的 `Python.app/Contents/MacOS/Python`，以真实 TCC subject 为准。重跑安装器会重新选择 PATH 中的 python3，须重新核对该配置。`EINTR` 或本地解密超时不能当作空窗口，也不能用反复重试代替授权。授权/实际后台验证失败时先 bootout/disable 两个任务，保留归档、checkpoint 与私有日志；不得绕过 TCC、改微信签名或自动联网补媒体。容器访问及全盘权限机制见 [Apple 容器保护说明](https://developer.apple.com/documentation/xcode/protecting-local-app-data-using-containers)与[文件访问控制说明](https://support.apple.com/guide/security/controlling-app-access-to-files-secddd1d86a6/web)。

当前验证和未通过的人工/Claude 门禁以 `docs/reports/phase-0..5.md` 为准；合成夹具通过不代表微信 UI 或真实问答已验收。

## 命令一览

| 命令 | 说明 |
|------|------|
| `wx-cli status` | 查看 WeChat 运行状态 |
| `wx-cli doctor` | 检查环境（SIP 等） |
| `wx-cli key extract` | LLDB hook 提取密钥 |
| `wx-cli key list` | 上游明文密钥查看接口；归档工作流禁止直接运行/粘贴 |
| `wx-cli key set <account> <key>` | 手动设置密钥 |
| `wx-cli key set-image <account> <image-key>` | 手动设置图片密钥 |
| `wx-cli decrypt` | 解密数据库 |
| `wx-cli sessions` | 最近会话列表 |
| `wx-cli contacts --search <名字>` | 搜索联系人 |
| `wx-cli query <联系人>` | 查询消息 |
| `wx-cli search <关键词>` | 全局搜索 |
| `wx-cli export <联系人>` | 导出会话 |
| `wx-cli watch` | 实时监听新消息 |
| `wx-cli decode-image <路径>` | 解密图片 |
| `wx-cli media extract-voice` | 提取语音 |
| `wx-cli media decrypt-video` | 解密视频号视频 |
| `wx-cli server run` | 启动 HTTP API 服务 |
| `wx-cli server status/stop/restart` | 管理服务 |
| `wx-cli paths` | 查看所有数据路径 |
| `wx-cli info <db>` | 查看数据库加密状态 |

## Contact Hiding

按账号隐藏指定联系人、群聊或带特定标签的联系人。启用后，查询、导出、监控等命令默认应用隐藏规则（全文搜索除外）。

配置文件：`~/Library/Application Support/wx-cli/config/settings.toml`

```toml
[accounts."<account_id>"]
ignore_contacts = ["wxid_xxx", "12345@chatroom"]
ignore_tags = ["同事", "客户"]
```

本地命令支持 `--show-hidden` 忽略隐藏规则查看完整结果。`search` 当前不会自动应用隐藏配置。

## 文件路径

| 类别 | 路径（macOS） | 用途 | 可删除？ |
|------|---------------|------|----------|
| Config | `~/Library/Application Support/wx-cli/config/` | 密钥、设置 | 否（先备份） |
| Cache | `~/Library/Caches/wx-cli/` | 解密后数据库 | 可（重新 decrypt） |
| State | `~/Library/Application Support/wx-cli/state/` | 服务运行时元数据 | 可 |
| Logs | `~/Library/Logs/wx-cli/` | 服务日志 | 可 |
| Temp | `$TMPDIR/wx-cli/` | 密钥提取临时文件 | 可 |

使用 `wx-cli paths` 查看所有路径。清理缓存：`rm -rf ~/Library/Caches/wx-cli/`。

## 项目结构

```
wx-cli/
├── crates/
│   ├── wx-decrypt/     # 核心解密库（KDF、逐页解密、整库解密）
│   ├── wx-keychain/    # 密钥提取（LLDB hook）与本地存储
│   ├── wx-cli/         # CLI 入口
│   ├── wx-db/          # 数据库查询（联系人、消息、会话、群聊）
│   ├── wx-media/       # 媒体解密（图片、语音、视频）
│   ├── wx-monitor/     # 实时消息监听与增量监控
│   ├── wx-context/     # 账号解析、解密缓存、联系人解析
│   └── wx-paths/       # 平台路径管理
```

## 常见问题

### `key extract` 超时

- 确认 WeChat 已弹出登录界面并完成登录
- 增加超时：`--timeout 300`
- 检查日志：`$TMPDIR/wx-cli/lldb/wx_cli_lldb_output.txt`

### SIP / DevToolsSecurity 报错

密钥提取需要 SIP 关闭。重启进入恢复模式执行 `csrutil disable`，然后运行 `wx-cli doctor` 逐项检查。

### 解密后数据库无法打开

- `wx-cli key list` 确认密钥正确
- `wx-cli info <db>` 检查文件是否为加密状态
- 确认 WeChat 版本不低于 4.1.7
