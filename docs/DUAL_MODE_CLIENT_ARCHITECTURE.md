# ZP-AC 双模式客户端验证架构

协议版本：`zp-ac-native-v1`  
兼容基线：Bedrock 1.26.60.6 / Protocol 2225 / Endstone 0.11.15 / Python 3.11

## 1. 双模式边界

### 标准 Bedrock 模式（默认，所有客户端可用）

- 行为包 Script API 在世界/服务端脚本环境运行，不是在每个远程玩家的客户端进程中运行。
- 因此这一模式准确名称是“服务端观察模式”，不是客户端本地扫描。
- 行为包和 Endstone 根据服务器实际观察到的移动、战斗、交互、方块事件和网络事件生成证据。
- 玩家无需安装专用 APK；不尝试读取客户端内存、进程、DLL 或本地文件。

### 原生客户端模式（可选）

- 专用 Android Bedrock 客户端集成 `native_client_sdk/android/NativeAttestationClient.kt`。
- Android Keystore 为每个玩家/设备生成 ECDSA P-256 私钥；私钥不离开 Keystore。
- 管理员为在线玩家生成一次性配对码，客户端用配对码和私钥证明持钥后登记公钥。
- 服务端每次验证发放一次性随机挑战、挑战 ID 和递增序列号。
- 客户端对完整 JSON 负载签名，服务端校验公钥、签名、UUID、挑战、过期时间和序列号。
- 成功后，服务端只把数据记为“签名验证通过的客户端遥测”。客户端完整性字段仍是客户端自报声明，不能单独证明没有被修改。

## 2. 数据流

1. 玩家先正常进入 Bedrock 服务器。
2. 管理员执行 `/zpac client pair <在线玩家名>`，得到 5 分钟有效的一次性配对码。
3. 专用客户端通过 HTTPS 调用 `POST /v1/enroll`，提交 UUID、公钥、key_id、配对码和持钥证明。
4. 客户端定期调用 `POST /v1/challenge` 获取随机 nonce、challenge_id、sequence。
5. 客户端将协议版本、UUID、设备 key_id、挑战、版本/build、能力列表、自检声明及遥测摘要组成规范化 JSON，并用 ECDSA P-256/SHA-256 签名。
6. 客户端调用 `POST /v1/attest`。服务端验证后写入 `native_client_registry.json` 和证据审计日志。
7. Endstone 将原生客户端结果与自身权威观察结果关联；当前版本不会仅凭客户端自报异常自动扣分、踢出或封禁。

## 3. HTTP API

所有 API 都必须通过 HTTPS 对外提供。插件默认只监听 `127.0.0.1:9179)，推荐由 Caddy/Nginx 反向代理 TLS。若插件直接绑定非 loopback 地址，必须同时配置 TLS 证书和私钥。

- `GET /v1/health`：返回协议健康状态，不泄露玩家信息。
- `POST /v1/enroll`：一次性配对与公钥登记。
- `POST /v1/challenge`：为在线且已配对玩家签发一次性挑战。
- `POST /v1/attest`：验证客户端签名、挑战和序列号。

请求体最大 16 KiB；每个来源 IP 默认最多 60 次请求/分钟。挑战默认 45 秒有效，配对码默认 300 秒有效。挑战、配对码均只保存在内存；已登记公钥和最近验证摘要保存在插件数据目录。撤销密钥不会删除游戏信用分或其他反作弊档案。

## 4. 管理命令

- `/zpac client status <玩家名|UUID>`：查看标准/原生模式、设备配对和最近签名验证状态。
- `/zpac client pair <在线玩家名>`：生成一次性配对码。管理员应通过可信私聊渠道交给该玩家，不要公开发送。
- `/zpac client revoke <玩家名|UUID>`：撤销已登记设备公钥。

## 5. 配置

在 Endstone 插件配置文件中：

```toml
native_attestation_enabled = true
native_attestation_bind_host = "127.0.0.1"
native_attestation_bind_port = 9179
native_attestation_tls_cert = ""
native_attestation_tls_key = ""
native_client_public_url = "https://你的验证域名"
native_attestation_challenge_ttl_seconds = 45
native_attestation_pairing_ttl_seconds = 300
```

使用反向代理时，插件内部监听 loopback 并保持证书字段为空；反向代理负责公网 TLS。不要将明文 HTTP 端口暴露到公网。

## 6. 原生 SDK 集成边界

Kotlin SDK 提供设备密钥生成、配对、挑战获取和签名提交，但它不会凭空读取 Bedrock 引擎内部状态。专用客户端必须在自身可信代码中接入输入、渲染、游戏网络和资源加载钩子，并把受控的汇总数据传给 `reportTelemetry()`。若只把 SDK 放进普通启动器而不接入游戏引擎，它只能证明该启动器持有设备密钥，不能检测游戏内外挂。

Android Keystore 的硬件保护能力取决于设备硬件和系统实现；`isHardwareBackedKey()` 只是本机查询结果。要获得更强的设备/应用证明，还需在服务端集成 Google Play Integrity 等远程证明服务，并验证其服务端令牌；本仓库当前不声称已经实现 Play Integrity。

## 7. 信任与处罚原则

- 标准模式与原生模式都执行同一套服务端权威检测。
- 客户端遥测是辅助证据；签名验证只证明登记私钥参与了签名，不证明客户端未被篡改。
- 缺少客户端响应、网络超时、未配对、客户端版本不兼容均不直接判定作弊。
- 任何客户端自报 `root_detected`、`debugger_detected`、`package_signature_valid` 等字段都不直接处罚。
- 自动处罚仍必须满足现有服务端规则、强证据和多规则交叉验证要求；永久封禁仍须管理员确认。

## 8. 交付与验证

Endstone 插件升级到 1.0.0，默认关闭原生 HTTP API，不影响普通 Bedrock 玩家。开启原生模式需要部署 HTTPS 反向代理并将 Kotlin SDK 集成进真正的专用客户端。CI 会测试配对、ECDSA 签名、挑战过期、重放拒绝、UUID 在线状态、密钥撤销和注册信息持久化。

CI 成功只证明代码检查和构建通过；仍需在 Bedrock 1.26.60.6 / Endstone 0.11.15 实服验证 API 可达性、客户端配对和设备 Keystore 行为。
