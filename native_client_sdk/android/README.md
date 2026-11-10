# Android 原生客户端 SDK 接入

此 SDK 是 ZP-AC 原生客户端验证的参考传输层，不是可独立安装的反作弊 APK，也不会自动读取 Minecraft 引擎内部数据。

## 集成前提

- 你正在开发/维护一个真正的专用 Android Bedrock 客户端，并能在客户端引擎层接入观测钩子。
- Android minSdk 建议 24 或更高。
- Manifest 声明 `android.permission.INTERNET`。
- 服务器必须提供有效证书的 HTTPS 地址；不要把明文 HTTP 暴露到公网。
- 不要在 Android 主线程调用 SDK；将网络调用放入 IO executor / coroutine。

## 配对

管理员先让玩家进入服务器，然后执行：

```text
/zpac client pair <玩家名>
```

将 5 分钟有效的一次性配对码通过可信私聊交给玩家。客户端：

```kotlin
val client = NativeAttestationClient(
    baseUrl = "https://ac.example.net",
    playerUuid = playerUuid,
    clientVersion = BuildConfig.VERSION_NAME,
    buildId = BuildConfig.VERSION_CODE.toString()
)
client.enroll(pairingCode)
```

SDK 在 Android Keystore 创建 P-256 密钥，向服务端登记公钥。私钥不会被导出。服务端把公钥绑定到当前玩家 UUID。

## 周期遥测

在客户端自己的引擎观测层汇总数据后，建议每 15-30 秒发送一次，不要每帧或每次点击发请求：

```kotlin
client.reportTelemetry(
    capabilities = listOf("input-telemetry", "render-state", "network-state"),
    integrityClaims = mapOf(
        "package_signature_valid" to packageSignatureCheck,
        "debugger_detected" to debuggerCheck,
        "hardware_backed_key" to client.isHardwareBackedKey()
    ),
    telemetry = JSONObject()
        .put("client_pos_x", clientPosition.x)
        .put("client_pos_y", clientPosition.y)
        .put("client_pos_z", clientPosition.z)
        .put("network_rtt_ms", measuredRttMs)
        .put("attack_input_rate", attackInputsPerSecond)
        .put("client_tick_ms", averageClientTickMs)
)
```

服务端目前只对 `client_pos_x/y/z` 与 `network_rtt_ms` 做双端位置差异的复核线索记录；其他数值作为签名遥测保存，不直接影响风险分或处罚。

## 必须理解的安全边界

- ECDSA 签名证明本次请求由登记私钥签署；它不证明 APK、游戏进程或游戏资源没有被修改。
- Android Keystore 是否硬件保护取决于设备；SDK 的 `isHardwareBackedKey()` 只是本机查询。
- `integrityClaims` 是客户端自报值，恶意客户端仍可能提交虚假状态。
- 不能把“未上报”“网络失败”“未配对”直接判定为作弊。
- 不可从普通行为包 Script API 读取远程玩家设备的内存、进程、DLL 或文件。
- 若需要更强的应用/设备证明，需另行接入并由服务端验证 Google Play Integrity 等远程证明令牌。

SDK 的网络方法是同步调用，必须放在后台线程。客户端引擎输入、位置、渲染和网络状态的真实采集，需要由专用客户端开发者在受控引擎层实现。
