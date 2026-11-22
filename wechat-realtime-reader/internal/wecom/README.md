# 企业微信 Go 读取引擎

2.0.2-dev 将原 Python 企业读取迁移为独立 Go 二进制 `cmd/wecom-reader`。数据格式依据原企业基线 `lamp-cat/Corporate-WeChat-Secretary@0b7cb53082f0f5302ac66c12c388cdc861ddf869`，继续通过继承管道与公共网关通信，不监听网络端口。

- `crypto.go`：企业格式 AES-128-CBC 页密钥及 IV、WAL 校验与已提交事务合并，与个人 SQLCipher 格式独立。
- `snapshot.go`：三库及 WAL 复制前后指纹一致性检查、只读源文件、0700/0600 独立副本与 SQLite quick_check。
- `content.go`：保留文本、JSON 字符串及已观测 protobuf 文本；未知结构返回明确占位标记。
- `query.go`：跨三消息表查询、解码后搜索、表名加消息号身份、兼容旧账号/会话游标、本人身份及类型未知值。
- `engine.go`：活跃账号发现、先恢复再验证钥匙串密钥、五秒同步、账号切换清理、管道请求与停止。
- `capture.go`、`hook.js`：Go 调用固定版本独立 Frida 工具，仅在本机管道接收候选密钥。临时副本复制/签名和暂停启动由连接引导触发。
- `setup/wecom-runtime.swift`：仅提供 macOS `posix_spawn` 暂停启动，固定官方数据目录环境；Go 等待监听就绪后解除暂停，失败也解除。
- `wecom_test.go`：加密、WAL、真实 SQLite、来源发现、钥匙串恢复次序、并发停止与取钥管道合成测试。

构建入口为 `app/build.sh`。公共 AI、MCP、归档及钥匙串持久化适配仍在 Python 网关。旧 Python 读取代码仅保留在 `tests/reference_wecom/` 作为查询对照样本，不进入安装包。

`wecom-reader --snapshot <三库目录> --account <测试账号>` 为显式离线验证模式，复制并校验指定数据，不发现或操作真实客户端。生产适配器不传入这些参数。密钥不通过命令行参数传递。

真实账号及客户端版本的取钥兼容性仍需实机验收。
