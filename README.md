# ZP-AC Bedrock

ZP-AC 是一套面向 Minecraft Bedrock 1.26.60 的反作弊系统，采用**一个行为包 + 一个 Endstone 增强插件**的交付结构。

- 行为包：2.1.3。无插件时独立运行，负责基础行为采样、本地历史记录、举报 UI、管理员面板和可配置检测开关。
- Endstone 增强插件：0.6.2。提供服务端原生事件检测、UUID 持久化画像、风险关联、待复核候选、可选自动踢出、人工确认永久封禁和可选云端 UUID 同步。
- 云端功能默认关闭；云端仅同步 UUID，不上传玩家名称、XUID、可信分、风险分或证据正文。

## 部署

1. 将构建产物 `ZP-AC-2.1.3.mcaddon` 导入 Minecraft/Bedrock 服务端，并在目标世界启用行为包。
2. 将 `endstone_zp_report_anticheat-0.6.2-py3-none-any.whl` 安装到 Endstone 0.11.15 / Python 3.11 环境。
3. 重启服务器，确认日志中出现 ZP-AC 0.6.2 启用信息和行为包握手信息。

兼容基线：Minecraft Bedrock 1.26.60.6、Protocol 2225、Endstone 0.11.15、Python 3.11、`@minecraft/server` 2.10.0、`@minecraft/server-ui` 2.2.0。

## 管理入口

- `/ba`：打开玩家举报界面。
- `/zp_ac:admin`：打开 ZP-AC 管理控制台。
- `/zpac`：Endstone 命令行管理入口。

行为包管理员 UI 通过标签授权。为非 OP 管理员添加标签：

```mcfunction
/tag <玩家名> add zp_ac_admin
```

安装 Endstone 插件后，插件会把 OP 状态镜像到保留标签 `zp_ac_op_admin`，使 OP 能直接打开管理 UI。请勿手动给普通玩家添加 `zp_ac_op_admin`；插件会按实际 OP 状态维护它。

管理员主菜单新增“反作弊检测功能开关”，管理员可单独启用/关闭移动速度、飞行/悬浮、超距攻击、多目标战斗、连点器、自动搭路和快速破坏方块检测。新安装默认全部启用；选择保存在世界动态属性中，插件在线时由管理员 UI 同步保存，插件重连时行为包会重新同步。快速破坏方块检测仅由 Endstone 增强层提供。原有插件增强管理菜单仍可查看待复核候选、查询隐藏画像、人工确认永久封禁、撤销 UUID 封禁和管理云端同步。

## 安全与处罚原则

- 举报数量本身不触发自动踢出。
- 单一检测项不直接导致永久封禁。
- 自动踢出需要多种规则、强证据和持续风险门槛共同满足。
- 永久封禁必须通过管理员明确确认。
- OP 和管理员标签账号默认豁免反作弊检测。
- 云端默认关闭；启用时要求 HTTPS（仅 localhost 开发测试允许 HTTP）。
- 玩家画像文件以 UUID 为键，只保存数值与规则统计；本地证据和候选文件可能包含玩家名称，仅保留在服务器本地数据目录。

## 通信与控制台日志

行为包与插件通过以下 ScriptEvent 通信：

- `zp:report_submitted`：举报消息。
- `zp:ac_plugin_ready`：插件心跳/就绪握手。
- `zp:ac_bp_evidence`：行为包检测证据。
- `zp:ac_admin_request` / `zp:ac_admin_response`：管理员 UI 请求与响应。
- `zp:ac_checks_sync`：行为包与 Endstone 同步管理员检测开关。

插件每 30 秒刷新一次握手，行为包使用 90 秒租约。该间隔用于明显降低控制台重复输出，同时保留插件离线检测和自动恢复能力。管理请求只允许在线 OP 或持有管理员标签的玩家，并在插件端再次验证。

## 本地构建

要求 Python 3.11+、Node.js 22+、可安装 setuptools / wheel / build。

```bash
python -m pip install --upgrade build
node --check --input-type=module < mcaddon/scripts/main.js
python -m compileall -q plugins/endstone_zp_anticheat
python tools/build_release.py
```

构建产物输出到 `dist/`，包含一个 `.mcaddon` 和一个 `.whl`。GitHub Actions 会在 main 分支更新后自动执行静态检查并上传这两个成品。

## 验收边界

CI 会执行 JavaScript 语法检查、Python 编译检查、清单/版本一致性检查，并生成两个发行包。CI 不模拟真实 Bedrock 客户端、Endstone 事件循环或多玩家服务器；最终验收仍需在实际 1.26.60.6 + Endstone 0.11.15 服务器中测试命令注册、举报、管理员 UI、封禁/撤销和云端连接。
