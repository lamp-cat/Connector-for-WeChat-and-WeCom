# 微信/企微连接器

读取个人微信和企业微信的本机消息数据，帮助团队在豆包与飞书生态中开展消息分析。当前支持本机检索、归档、会话 AI 分析和只读 MCP；豆包工作提供接入检测与手动配置指引，飞书任务或台账写回仍属于扩展能力。

同一个 macOS App 内切换「个人微信」和「企业微信」，共用 AI 服务、设置页、鉴权与浮窗。两种来源都支持只读 MCP 和本机消息归档。当前版本：`2.0.3-dev`，两种消息读取引擎均使用 Go。

统一版仓库：[lamp-cat/Connector-for-WeChat-and-WeCom](https://github.com/lamp-cat/Connector-for-WeChat-and-WeCom)。个人版基线为 [lamp-cat/wechat-secretary](https://github.com/lamp-cat/wechat-secretary) 的 `6653024325c3cf033bc6179a6af05fd15a20c3e0`；企业版基线为 [lamp-cat/Corporate-WeChat-Secretary](https://github.com/lamp-cat/Corporate-WeChat-Secretary) 的 `0b7cb53082f0f5302ac66c12c388cdc861ddf869`。统一版以独立仓库维护，保留两版上游来源。

## 打开新版

下载开发预览版：[微信/企微连接器 v2.0.3-dev](https://github.com/lamp-cat/Connector-for-WeChat-and-WeCom/releases/tag/v2.0.3-dev)。

从仓库获取源码后，先按下方「开发与验证」构建，安装包会生成在 `app/dist/`。

1. 打开 `app/dist/微信-企微连接器.dmg`，将 App 拖入「应用程序」；开发时也可直接打开 `app/dist/微信-企微连接器.app`。产品展示名为「微信/企微连接器」，文件名以连字符代替路径分隔符 `/`。
2. 首次启动准备公共 Python 环境和网关，无需先连接个人微信。首次安装依赖需要联网。
3. 在浮窗或设置页选择来源。个人微信沿用原有读取器配置；尚未连接时，通过设置页初始化。企业微信通过「首次连接企业微信…」连接。
4. 在「AI 服务」配置兼容 OpenAI 的 Base URL、API Key、模型。两种来源使用同一套配置。
5. 按需开启当前账号的归档。企业微信 MCP 需要在当前来源设置页单独授权，再登记到 agent 工具。

App 支持 Apple Silicon、macOS 13 及以上。安装包使用本机临时签名，尚未进行 Developer ID 签名和 Apple 公证。真实账号连接与真实模型质量尚未验收，详见 [开发与验证报告](docs/开发与验证报告.md)。

新名称的产品界面：[个人微信浮窗](docs/产品截图/01-个人微信浮窗.png) · [企业微信总结与草稿](docs/产品截图/02-企业微信总结与草稿.png)。截图使用合成消息和 AI 测试替身；本轮检查见 [命名更新与验证](docs/命名更新与验证.md)。

## 已整合的能力

查看 [当前项目已经具备哪些功能](docs/项目介绍与客户场景分析.md#3-当前项目已经具备哪些功能)，内有两种来源的读取范围对照图、产品截图和豆包工作接入示例。

| 能力 | 个人微信 | 企业微信 |
|---|---|---|
| 来源切换、浮窗、共享 AI 设置 | 支持 | 支持 |
| 会话、消息、搜索、分页 | 沿用原 Go 读取器 | Go 三库快照查询，使用不透明游标 |
| 总结、待办线索、回复草稿 | 共享网关 AI | 共享网关 AI |
| MCP | 原有 10 个只读工具 | 6 个只读工具，独立授权 |
| SQLite、Markdown 归档 | 按账号隔离，兼容旧目录 | 按账号隔离，包含文本及结构化信息 |
| 图片、视频、语音 | 沿用原能力及其限制 | 暂未接入媒体本体 |
| 会话类型、本人身份、成员 | 沿用原解析 | 类型与本人身份显示未知，成员暂不支持 |

企业微信浮窗使用所选会话或最近活跃会话作为估计，不声称识别当前官方客户端前台会话。手动选定会话可以锁定。

## 数据路径

```text
Swift 原生 App
        │
公共 FastAPI 网关 127.0.0.1:8088
        ├── 个人微信：Go 读取器 127.0.0.1:5678 → 解密后的独立快照
        └── 企业微信：Go wecom-reader 本机管道 → message/session/user 三库独立快照
        │
        ├── 共用 AI：只使用导出的消息文本，生成总结及草稿
        ├── MCP：来源独立，企业微信会话固定账号
        └── 归档：来源 + 账号目录，SQLite + Markdown
```

原数据库只读，不写回；WAL 只在快照副本中合并。不模拟登录、不伪造协议、不修改官方客户端安装，不关闭 SIP。首次取钥可能使用经确认的临时客户端副本；企业微信准备副本与退出启动分别确认，未正常退出时取消启动。

企业微信的账号发现、逐页 AES-128-CBC 解密、WAL 校验与合并、SQLite 查询、protobuf 文本解析均在 `wechat-realtime-reader/internal/wecom/` 中实现。Go 调用随包分发的独立 Frida 工具；Swift helper 仅提供钥匙串及暂停启动原语。临时副本在取钥监听就绪后继续运行；连接失败也会解除暂停。AI、归档及 MCP 仍由公共 Python 网关提供，企业读取无需 Python Crypto/Frida 依赖。

数据库密钥仅存本机权限 `0600` 文件或系统钥匙串；不会写入 agent 配置或归档。服务只监听回环地址，读取器 HTTP 客户端拒绝外部地址。没有聊天发送接口。

归档保存在本机。配置外部 AI 后，选定的消息文本会发往你配置的模型接口；模型不会直连聊天数据库。归档中的内部 ID 会散列处理，但姓名、会话名及原文仍可能包含个人信息，不构成完整匿名化。

## MCP

个人微信保留服务名 `wechat-reader`，stdio 使用 `wechat-reader mcp`，HTTP 地址为 `http://127.0.0.1:5678/mcp`。

企业微信服务名为 `wecom-reader`，HTTP 地址为 `http://127.0.0.1:8088/mcp/wecom`。专用令牌保存在 `~/.config/wechat-frontend/mcp/wecom-token`，权限 `0600`；该令牌不授予设置页或个人微信读取权限。关闭授权立即拒绝旧令牌，重新开启会生成新令牌。

企业微信工具：`list_sessions`、`search_contacts`、`get_messages`、`search_messages`、`get_new_messages`、`sync_status`。工具均只读。

设置页可生成准确的 stdio 配置。手动配置时使用公共运行时的 Python：

```json
{
  "mcpServers": {
    "wecom-reader": {
      "command": "/Users/<用户名>/.local/share/wechatdesk/runtime/wechat-frontend/.venv/bin/python",
      "args": ["/Users/<用户名>/.local/share/wechatdesk/runtime/wechat-frontend/app/mcp_stdio.py", "--source", "wecom"],
      "env": {"WF_CONFIG": "/Users/<用户名>/.config/wechat-frontend/config.json"}
    }
  }
}
```

工具列表包含「豆包工作」，提供安装检测、接入说明及按当前来源复制配置。当前未验证豆包工作支持本机 stdio/HTTP 接入，因此显示为「手动接入」，不会自动写入未经确认的客户端配置，也不会把安装检测当作连接成功。如果豆包工作只接受远程 HTTPS 服务，本项目的本机 MCP 无法直接接入。

切换浮窗来源不改变 MCP 的来源。企业微信 MCP 首次读取绑定账号，账号变化后该会话返回错误，需要重新初始化。登记和撤销只处理对应服务项，原 agent 配置会备份到 `~/.config/wechatdesk/backups/`。

## 归档与旧版升级

新归档默认目录：

```text
~/MessageArchive/personal/<账号哈希>/
~/MessageArchive/wecom/<账号哈希>/
```

各账号独立保存开关、会话范围与同步进度。已开启任务不随浮窗切换来源；当前账号离线或变化时，绑定任务拒绝跨账号读取。

个人版旧 `archive-config.json`、归档目录及附件会保留。旧数据库升级会补充稳定的 `message_key`，保留消息行号和附件关联。由于旧版未记录归档所属账号，设置页会提示核对账号，再通过原有启用确认恢复同步；确认前暂停写入。也可选择新目录开始独立归档。

企业微信消息身份包含来源表名及消息号；跨表同号消息分别保留。分页游标仅适用于生成它的来源、账号、会话和查询范围，不要自行解析或跨账号复用。

## 开发与验证

```bash
# 源码构建；需要 Xcode Command Line Tools、Python 3 和 Go 1.27.3+
bash app/build.sh

# Python 回归测试
python3 -m venv /tmp/chat-companion-dev-venv
/tmp/chat-companion-dev-venv/bin/python -m pip install -r wechat-frontend/requirements.txt -r tests/requirements.txt
/tmp/chat-companion-dev-venv/bin/python -m pytest tests -q
node tests/test_mcp_config.js

# 两种 Go 读取器测试
cd wechat-realtime-reader
go test ./...
go test -race ./internal/wecom
go build -o bin/wecom-reader ./cmd/wecom-reader
```

`tests/ui_fixture_server.py` 可在 `127.0.0.1:18088` 启动合成聊天验收环境，测试令牌为 `ui-fixture-login`。该入口不会访问真实客户端、钥匙串或 launchd；不能用于真实账号。正常应用入口仍为打包后的 App。

构建会生成 `.app`、中文文件名 DMG 及 `WeChatWeComConnector-v2.0.3-dev.dmg`，同时内嵌个人与企业 Go 二进制。Frida 官方独立工具的固定版本和 SHA-256 校验沿用个人版；首次源码构建会下载，许可证及来源位于 `wechat-realtime-reader/skills/wechat-db-keys/vendor/frida/`。旧 Python 读取实现仅作为 `tests/reference_wecom/` 中的对照样本保留，不进入安装包。测试前须构建企业 Go 二进制和原生 helper（执行 `bash app/build.sh`）。

目录说明及实现边界见 [开发与验证报告](docs/开发与验证报告.md)，更新记录见 [CHANGELOG.md](CHANGELOG.md)。
