import {
  world,
  system,
  GameMode,
  ScriptEventSource,
  EntitySwingSource,
  Player,
  CommandPermissionLevel,
  CustomCommandStatus
} from "@minecraft/server";
import { ActionFormData, MessageFormData, ModalFormData } from "@minecraft/server-ui";

/*
 * ZP-AC Bedrock Core | Minecraft Bedrock 1.26.60
 * Behavior Pack 2.1.2 | @minecraft/server 2.10.0
 *
 * Design:
 * - Standalone mode: this behavior pack continuously monitors online players,
 *   records evidence in world dynamic properties and alerts tagged admins.
 * - Endstone enhanced mode: an authenticated-in-process heartbeat is exchanged
 *   through server ScriptEvents. Evidence is forwarded to Endstone for durable
 *   audit, correlation scoring, optional kick, review candidates and UUID bans.
 * - No network calls, no client-memory inspection, no automatic permanent bans.
 */

const CONFIG = Object.freeze({
  ADMIN_TAG: "zp_ac_admin",
  BYPASS_TAG: "zp_ac_bypass",
  REPORT_EVENT: "zp:report_submitted",
  PLUGIN_READY_EVENT: "zp:ac_plugin_ready",
  PLUGIN_EVIDENCE_EVENT: "zp:ac_bp_evidence",
  ADMIN_REQUEST_EVENT: "zp:ac_admin_request",
  ADMIN_RESPONSE_EVENT: "zp:ac_admin_response",
  ADMIN_PROTOCOL: "zp-ac-admin-v1",
  OP_ADMIN_TAG: "zp_ac_op_admin",
  PROTOCOL: "zp-ac-bp-v1",

  SAMPLE_INTERVAL: 5,
  DISCONNECT_GRACE_TICKS: 20 * 8,
  ALERT_COOLDOWN_TICKS: 20 * 30,
  PLUGIN_LEASE_TICKS: 20 * 90,

  HISTORY_KEY: "zp_ac_history_v2",
  HISTORY_LIMIT: 80,
  HISTORY_MAX_CHARS: 28000,

  SPEED_NORMAL: 12.8,
  SPEED_WITH_POTION: 15.2,
  SPEED_REQUIRED_SAMPLES: 3,
  TELEPORT_DELTA: 24,
  HOVER_TICKS: 20 * 3,
  ASCEND_TICKS: 20 * 2,

  REACH_HARD: 7.8,
  REACH_SOFT: 6.2,
  REACH_SOFT_COUNT: 3,
  HIT_WINDOW_MS: 3000,
  HIT_RATE_COUNT: 8,
  AURA_WINDOW_MS: 2000,
  AURA_HIT_COUNT: 8,
  AURA_TARGET_COUNT: 3,

  SWING_WINDOW_MS: 3000,
  SWING_COUNT: 48,
  SWING_MIN_INTERVALS: 30,
  SWING_MAX_CV: 0.12,

  SCAFFOLD_WINDOW_MS: 3500,
  SCAFFOLD_PLACEMENTS: 8,
  SCAFFOLD_MIN_TRAVEL: 3.5,
  SCAFFOLD_MAX_Y_SPREAD: 1.25
});

const sessions = new Map();
let tickCounter = 0;
let history = [];
let pluginLeaseUntil = 0;
let pluginModeAnnounced = false;

function cleanText(value, max = 180) {
  return String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, max);
}

function nowMs() {
  return Date.now();
}

function distance3(a, b) {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  const dz = a.z - b.z;
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

function horizontalDistance(a, b) {
  const dx = a.x - b.x;
  const dz = a.z - b.z;
  return Math.sqrt(dx * dx + dz * dz);
}

function coords(player) {
  const p = player.location;
  return `${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)}`;
}

function isPluginOnline() {
  return tickCounter <= pluginLeaseUntil;
}

function admins() {
  try {
    return world.getPlayers().filter((p) =>
      p.hasTag(CONFIG.ADMIN_TAG) || p.hasTag("zp:ac_admin")
    );
  } catch {
    return [];
  }
}

function notifyAdmins(message) {
  const line = `§8[§cZP-AC§8] §r${message}`;
  for (const admin of admins()) {
    try { admin.sendMessage(line); } catch { /* admin may be leaving */ }
  }
}

function saveHistory() {
  try {
    let entries = history.slice(-CONFIG.HISTORY_LIMIT);
    let serialized = JSON.stringify(entries);
    // 动态属性字符串有长度上限；宁可保留较少的最新记录，也不要让保存反复失败。
    while (serialized.length > CONFIG.HISTORY_MAX_CHARS && entries.length > 1) {
      entries.shift();
      serialized = JSON.stringify(entries);
    }
    world.setDynamicProperty(CONFIG.HISTORY_KEY, serialized);
  } catch (e) {
    console.warn(`[ZP-AC] 保存历史记录失败: ${e}`);
  }
}

function addHistory(entry) {
  history.push({ time: new Date().toISOString(), ...entry });
  if (history.length > CONFIG.HISTORY_LIMIT) history = history.slice(-CONFIG.HISTORY_LIMIT);
  saveHistory();
}

function loadHistory() {
  try {
    const raw = world.getDynamicProperty(CONFIG.HISTORY_KEY);
    if (typeof raw === "string") {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) history = parsed.slice(-CONFIG.HISTORY_LIMIT);
    }
  } catch (e) {
    console.warn(`[ZP-AC] 读取历史记录失败: ${e}`);
  }
}

function runServerEvent(eventId, message) {
  try {
    const encoded = encodeURIComponent(String(message ?? ""));
    const result = world.getDimension("overworld").runCommand(`scriptevent ${eventId} ${encoded}`);
    return !result || result.successCount !== 0;
  } catch (e) {
    console.warn(`[ZP-AC] ScriptEvent ${eventId} 发送失败: ${e}`);
    return false;
  }
}

function sendEvidenceToPlugin(payload) {
  if (!isPluginOnline()) return false;
  const message = JSON.stringify({
    version: CONFIG.PROTOCOL,
    targetName: cleanText(payload.targetName, 64),
    targetId: cleanText(payload.targetId, 128),
    code: cleanText(payload.code, 48),
    severity: cleanText(payload.severity, 12),
    title: cleanText(payload.title, 80),
    detail: cleanText(payload.detail, 260),
    highConfidence: Boolean(payload.highConfidence),
    dimension: cleanText(payload.dimension, 48),
    location: cleanText(payload.location, 64)
  });
  return runServerEvent(CONFIG.PLUGIN_EVIDENCE_EVENT, message);
}

function hasEffect(player, effectId) {
  try { return player.getEffect(effectId) !== undefined; } catch { return false; }
}

function isFlightExempt(player) {
  let mode;
  try { mode = player.getGameMode(); } catch { mode = undefined; }
  return mode === GameMode.Creative ||
    mode === GameMode.Spectator ||
    player.isFlying === true ||
    player.isGliding === true ||
    hasEffect(player, "levitation") ||
    hasEffect(player, "slow_falling");
}

function isExempt(player) {
  try {
    if (
      player.hasTag(CONFIG.ADMIN_TAG) ||
      player.hasTag("zp:ac_admin") ||
      player.hasTag(CONFIG.BYPASS_TAG) ||
      player.hasTag("zp:ac_bypass")
    ) return true;
  } catch { /* tag APIs may be unavailable during early join */ }
  try {
    const mode = player.getGameMode();
    return mode === GameMode.Creative || mode === GameMode.Spectator;
  } catch {
    return false;
  }
}

function createSession(player) {
  const loc = player.location;
  return {
    playerId: player.id,
    playerName: cleanText(player.name, 64),
    dimensionId: player.dimension.id,
    startedAt: tickCounter,
    lastEvidenceAt: tickCounter,
    lastSeenAt: tickCounter,
    reportCount: 0,
    reporters: [],
    reasons: [],
    reportIds: [],
    lastLocation: { x: loc.x, y: loc.y, z: loc.z },
    lastSampleTick: tickCounter,
    speedStrikes: 0,
    hoverTicks: 0,
    hoverTravel: 0,
    ascendTicks: 0,
    lastAlertAt: Object.create(null),
    hitEvents: [],
    reachSuspects: [],
    swingTimes: [],
    scaffoldEvents: []
  };
}

function getSession(player) {
  if (isExempt(player)) {
    sessions.delete(player.id);
    return undefined;
  }
  let session = sessions.get(player.id);
  if (!session) {
    session = createSession(player);
    sessions.set(player.id, session);
    addHistory({
      type: "player_monitor_start",
      target: session.playerName,
      targetId: session.playerId
    });
  }
  session.lastSeenAt = tickCounter;
  return session;
}

function startMonitoring(player, report) {
  const session = getSession(player);
  if (!session) {
    notifyAdmins(`忽略对管理/豁免玩家 ${cleanText(player.name, 64)} 的举报。`);
    return;
  }

  session.reportCount += 1;
  const reporterName = cleanText(report.reporterName, 64);
  const reason = cleanText(report.reason, 180);
  const reportId = cleanText(report.reportId, 80);
  if (reporterName && !session.reporters.includes(reporterName)) {
    session.reporters.push(reporterName);
    session.reporters = session.reporters.slice(-8);
  }
  if (reason && !session.reasons.includes(reason)) {
    session.reasons.push(reason);
    session.reasons = session.reasons.slice(-8);
  }
  if (reportId) session.reportIds = [...session.reportIds, reportId].slice(-20);
  session.lastEvidenceAt = tickCounter;

  addHistory({
    type: "report_received",
    target: session.playerName,
    targetId: session.playerId,
    reporter: reporterName || "未知",
    reason: reason || "未填写",
    reportId
  });

  if (!isPluginOnline()) {
    notifyAdmins(`收到举报并提高观察优先级：§e${session.playerName}§r；累计举报 ${session.reportCount} 次${reason ? `；原因：${reason}` : ""}。全员基础检测始终运行。`);
  }
}

function flag(session, player, code, severity, title, detail, highConfidence = false) {
  const cooldown = code === "scaffold" || code === "autoclick" ? 20 * 60 : CONFIG.ALERT_COOLDOWN_TICKS;
  const last = session.lastAlertAt[code] ?? -999999;
  if (tickCounter - last < cooldown) return;
  session.lastAlertAt[code] = tickCounter;
  session.lastEvidenceAt = tickCounter;

  const location = coords(player);
  const safeDetail = cleanText(detail, 260);
  const evidence = {
    type: "flag",
    target: session.playerName,
    targetId: session.playerId,
    code,
    severity,
    title,
    detail: safeDetail,
    dimension: player.dimension.id,
    location
  };
  addHistory(evidence);

  const forwarded = sendEvidenceToPlugin({
    targetName: session.playerName,
    targetId: session.playerId,
    code,
    severity,
    title,
    detail: safeDetail,
    highConfidence,
    dimension: player.dimension.id,
    location
  });

  if (!forwarded) {
    notifyAdmins(`§c${severity}异常§r：§e${session.playerName}§r | ${title} | ${safeDetail} | ${player.dimension.id} (${location})。该证据仅供复核，不代表单项即确认为作弊。`);
  }
}

function sampleMovement(player, session) {
  const location = player.location;
  const dimensionId = player.dimension.id;
  const elapsedTicks = Math.max(1, tickCounter - session.lastSampleTick);
  const old = session.lastLocation;

  if (dimensionId !== session.dimensionId) {
    session.dimensionId = dimensionId;
    session.lastLocation = { x: location.x, y: location.y, z: location.z };
    session.lastSampleTick = tickCounter;
    session.speedStrikes = 0;
    session.hoverTicks = 0;
    session.hoverTravel = 0;
    session.ascendTicks = 0;
    return;
  }

  const dx = location.x - old.x;
  const dy = location.y - old.y;
  const dz = location.z - old.z;
  const horizontal = Math.sqrt(dx * dx + dz * dz);
  const fullDistance = Math.sqrt(dx * dx + dy * dy + dz * dz);
  const seconds = elapsedTicks / 20;
  const horizontalSpeed = horizontal / seconds;

  if (fullDistance >= CONFIG.TELEPORT_DELTA) {
    flag(session, player, "large_displacement", "信息", "检测到大幅位移", `采样间隔 ${elapsedTicks} tick，位移 ${fullDistance.toFixed(1)} 格；传送、击退、区块/网络异常均可能造成此现象，仅记录不计入处罚。`);
    session.speedStrikes = 0;
    session.hoverTicks = 0;
    session.hoverTravel = 0;
    session.ascendTicks = 0;
  } else {
    const speedBoost = hasEffect(player, "speed");
    const speedLimit = speedBoost ? CONFIG.SPEED_WITH_POTION : CONFIG.SPEED_NORMAL;
    const exemptMove = isFlightExempt(player) || player.isInWater || player.isClimbing;

    if (!exemptMove && horizontalSpeed > speedLimit && fullDistance > 1.0) {
      session.speedStrikes += 1;
      if (session.speedStrikes >= CONFIG.SPEED_REQUIRED_SAMPLES) {
        flag(session, player, "speed", "高", "持续高速移动异常", `连续 ${session.speedStrikes} 次采样超过保守阈值；当前约 ${horizontalSpeed.toFixed(1)} 格/秒，阈值 ${speedLimit.toFixed(1)} 格/秒。`, true);
        session.speedStrikes = 0;
      }
    } else {
      session.speedStrikes = Math.max(0, session.speedStrikes - 1);
    }

    let onGround = true;
    let falling = true;
    try {
      onGround = player.isOnGround;
      falling = player.isFalling;
    } catch { /* unavailable state: skip air checks */ }

    const flightExempt = isFlightExempt(player) || player.isInWater || player.isClimbing;
    if (!flightExempt && onGround === false && falling === false && Math.abs(dy) < 0.045) {
      session.hoverTicks += elapsedTicks;
      session.hoverTravel += horizontal;
      if (session.hoverTicks >= CONFIG.HOVER_TICKS && session.hoverTravel >= 0.9) {
        flag(session, player, "hover", "高", "疑似持续空中悬浮", `离地约 ${(session.hoverTicks / 20).toFixed(1)} 秒，垂直位移近乎为零，水平移动 ${session.hoverTravel.toFixed(1)} 格；已排除常见飞行/药水/水中状态。`, true);
        session.hoverTicks = 0;
        session.hoverTravel = 0;
      }
    } else {
      session.hoverTicks = 0;
      session.hoverTravel = 0;
    }

    if (!flightExempt && onGround === false && !player.isInWater && !player.isClimbing && dy > 0.16) {
      session.ascendTicks += elapsedTicks;
      if (session.ascendTicks >= CONFIG.ASCEND_TICKS) {
        flag(session, player, "ascending", "中", "持续异常上升线索", `连续约 ${(session.ascendTicks / 20).toFixed(1)} 秒保持明显向上位移；跳跃、弹射物、击退和特殊地图机制仍需排除。`, false);
        session.ascendTicks = 0;
      }
    } else {
      session.ascendTicks = 0;
    }
  }

  session.lastLocation = { x: location.x, y: location.y, z: location.z };
  session.lastSampleTick = tickCounter;
}

function trackSwing(event) {
  const player = event.player;
  if (!player || player.typeId !== "minecraft:player") return;
  if (event.swingSource !== EntitySwingSource.Attack) return;
  const session = getSession(player);
  if (!session) return;

  const now = nowMs();
  session.swingTimes.push(now);
  session.swingTimes = session.swingTimes.filter((time) => now - time <= CONFIG.SWING_WINDOW_MS);
  if (session.swingTimes.length < CONFIG.SWING_COUNT) return;

  const intervals = [];
  for (let i = 1; i < session.swingTimes.length; i += 1) {
    intervals.push(session.swingTimes[i] - session.swingTimes[i - 1]);
  }
  if (intervals.length < CONFIG.SWING_MIN_INTERVALS) return;
  const mean = intervals.reduce((sum, value) => sum + value, 0) / intervals.length;
  if (mean <= 0) return;
  const variance = intervals.reduce((sum, value) => sum + ((value - mean) ** 2), 0) / intervals.length;
  const cv = Math.sqrt(variance) / mean;
  if (cv <= CONFIG.SWING_MAX_CV) {
    flag(session, player, "autoclick", "低", "高频且高度规律的攻击挥臂", `最近 ${CONFIG.SWING_WINDOW_MS / 1000} 秒记录到 ${session.swingTimes.length} 次 Attack 摆臂；平均间隔 ${mean.toFixed(1)} ms，间隔变异系数 ${cv.toFixed(3)}。仅为连点器线索，不能单独定罪。`, false);
    session.swingTimes = session.swingTimes.slice(-12);
  }
}

function trackHit(event) {
  const attacker = event.damagingEntity;
  if (!attacker || attacker.typeId !== "minecraft:player") return;
  const session = getSession(attacker);
  if (!session) return;

  const target = event.hitEntity;
  const isPlayerTarget = target?.typeId === "minecraft:player";
  let reach;
  try { reach = distance3(attacker.location, target.location); } catch { reach = undefined; }
  const now = nowMs();

  session.hitEvents.push({
    at: now,
    targetId: target?.id || "unknown",
    targetName: isPlayerTarget ? cleanText(target.name, 64) : cleanText(target?.typeId || "entity", 64),
    isPlayer: isPlayerTarget,
    reach
  });
  session.hitEvents = session.hitEvents.filter((hit) => now - hit.at <= CONFIG.HIT_WINDOW_MS);

  if (typeof reach === "number" && isPlayerTarget) {
    if (reach >= CONFIG.REACH_HARD) {
      flag(session, attacker, "reach_hard", "中", "疑似超距近战命中", `命中玩家 ${cleanText(target.name, 64)} 时双方位置距离约 ${reach.toFixed(2)} 格；该值是实体位置距离近似，不是精确碰撞箱/视线距离。`, false);
    } else if (reach >= CONFIG.REACH_SOFT) {
      session.reachSuspects.push({ at: now, reach, target: cleanText(target.name, 64) });
      session.reachSuspects = session.reachSuspects.filter((hit) => now - hit.at <= 5000);
      if (session.reachSuspects.length >= CONFIG.REACH_SOFT_COUNT) {
        const maxReach = Math.max(...session.reachSuspects.map((hit) => hit.reach));
        flag(session, attacker, "reach_repeat", "中", "重复出现远距离近战命中线索", `${session.reachSuspects.length} 次命中距离达到 ${CONFIG.REACH_SOFT} 格以上；最大约 ${maxReach.toFixed(2)} 格。需结合延迟和击退复核。`, false);
        session.reachSuspects = [];
      }
    }
  }

  const recentPlayers = session.hitEvents.filter(
    (hit) => hit.isPlayer && now - hit.at <= CONFIG.AURA_WINDOW_MS
  );
  const targetIds = new Set(recentPlayers.map((hit) => hit.targetId));
  if (recentPlayers.length >= CONFIG.HIT_RATE_COUNT && targetIds.size >= CONFIG.AURA_TARGET_COUNT) {
    flag(session, attacker, "multi_target", "中", "短时间多目标连续命中", `约 ${CONFIG.AURA_WINDOW_MS / 1000} 秒内检测到多目标命中模式，命中玩家 ${recentPlayers.length} 次，涉及 ${targetIds.size} 名不同玩家；团战环境可能产生相似行为。`, false);
  }
}

function trackPlace(event) {
  const player = event.player;
  if (!player || player.typeId !== "minecraft:player") return;
  const session = getSession(player);
  if (!session) return;

  let blockLocation;
  try { blockLocation = event.block.location; } catch { return; }
  const playerLocation = player.location;
  const footY = Math.floor(playerLocation.y);
  if (blockLocation.y > footY + 0.5 || blockLocation.y < footY - 2) return;

  const now = nowMs();
  session.scaffoldEvents.push({
    at: now,
    block: { x: blockLocation.x, y: blockLocation.y, z: blockLocation.z },
    player: { x: playerLocation.x, y: playerLocation.y, z: playerLocation.z }
  });
  session.scaffoldEvents = session.scaffoldEvents.filter((entry) => now - entry.at <= CONFIG.SCAFFOLD_WINDOW_MS);
  if (session.scaffoldEvents.length < CONFIG.SCAFFOLD_PLACEMENTS) return;

  const events = session.scaffoldEvents;
  const first = events[0].player;
  const last = events[events.length - 1].player;
  const travel = horizontalDistance(first, last);
  const ys = events.map((entry) => entry.block.y);
  const ySpread = Math.max(...ys) - Math.min(...ys);
  const uniqueBlocks = new Set(events.map((entry) => `${entry.block.x},${entry.block.y},${entry.block.z}`));

  if (travel >= CONFIG.SCAFFOLD_MIN_TRAVEL && ySpread <= CONFIG.SCAFFOLD_MAX_Y_SPREAD && uniqueBlocks.size >= CONFIG.SCAFFOLD_PLACEMENTS) {
    flag(session, player, "scaffold", "低", "移动中连续脚边放置方块", `${events.length} 次脚边/身侧放置发生在 ${(CONFIG.SCAFFOLD_WINDOW_MS / 1000).toFixed(1)} 秒窗口内，玩家移动 ${travel.toFixed(1)} 格，放置高度差 ${ySpread.toFixed(1)} 格。正常桥接/跑酷也可能相似，仅作辅助线索。`, false);
    session.scaffoldEvents = session.scaffoldEvents.slice(-2);
  }
}

function findOnlineTarget(data) {
  const online = world.getPlayers();
  if (data.targetId) {
    const byId = online.find((p) => p.id === String(data.targetId));
    if (byId) return byId;
  }
  const wanted = cleanText(data.targetName, 64).toLocaleLowerCase();
  if (!wanted) return undefined;
  return online.find((p) => p.name.toLocaleLowerCase() === wanted);
}

function decodeBase64Utf8(input) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const clean = String(input || "").replace(/\s/g, "");
  const bytes = [];
  let buffer = 0;
  let bits = 0;
  for (const char of clean) {
    if (char === "=") break;
    const value = alphabet.indexOf(char);
    if (value < 0) continue;
    buffer = (buffer << 6) | value;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((buffer >> bits) & 255);
    }
  }
  const escaped = bytes.map((byte) => `%${byte.toString(16).padStart(2, "0")}`).join("");
  return decodeURIComponent(escaped);
}

function parseReportMessage(raw) {
  let message = String(raw || "");
  if (message.startsWith("b64:")) {
    message = decodeBase64Utf8(message.slice(4));
  } else {
    try { return JSON.parse(message); } catch { /* attempt URL decoding */ }
    message = decodeURIComponent(message);
  }
  return JSON.parse(message);
}

function handleScriptEvent(event) {
  if (event.id === CONFIG.ADMIN_RESPONSE_EVENT) {
    handleAdminResponse(event);
    return;
  }

  if (event.id === CONFIG.PLUGIN_READY_EVENT) {
    if (event.sourceType !== ScriptEventSource.Server) return;
    if (String(event.message || "").trim() === CONFIG.PROTOCOL) {
      pluginLeaseUntil = tickCounter + CONFIG.PLUGIN_LEASE_TICKS;
      if (!pluginModeAnnounced) {
        pluginModeAnnounced = true;
        notifyAdmins("§a已检测到 ZP-AC Endstone 增强插件：证据将同步至插件评分/审计层；基础行为包检测仍持续运行。§r");
      }
    }
    return;
  }

  if (event.id !== CONFIG.REPORT_EVENT) return;
  if (event.sourceType !== ScriptEventSource.Server) {
    console.warn(`[ZP-AC] 拒绝非服务端来源的举报事件: ${event.sourceType}`);
    return;
  }

  let data;
  try {
    data = parseReportMessage(event.message);
  } catch (e) {
    notifyAdmins("收到无法解析的举报消息；独立基础监测仍然运行，请检查举报附加包通信。 ");
    console.warn(`[ZP-AC] 举报消息解析失败: ${e}`);
    return;
  }

  if (!data || typeof data !== "object") return;
  if (data.version && data.version !== "zp-report-v1") return;

  const report = {
    targetId: cleanText(data.targetId, 128),
    targetName: cleanText(data.targetName, 64),
    reporterName: cleanText(data.reporterName, 64),
    reason: cleanText(data.reason, 180),
    reportId: cleanText(data.reportId, 80)
  };
  const player = findOnlineTarget(report);
  if (!player) {
    notifyAdmins(`收到针对 §e${report.targetName || "未知玩家"}§r 的举报，但目标当前不在线；全员基础检测仍保持运行。`);
    addHistory({ type: "report_target_offline", target: report.targetName || "未知", reporter: report.reporterName || "未知" });
    return;
  }
  startMonitoring(player, report);
}

function monitoringLoop() {
  tickCounter += CONFIG.SAMPLE_INTERVAL;
  let online;
  try { online = world.getPlayers(); } catch { return; }

  const onlineIds = new Set();
  for (const player of online) {
    onlineIds.add(player.id);
    if (isExempt(player)) {
      sessions.delete(player.id);
      continue;
    }
    const session = getSession(player);
    if (!session) continue;
    try {
      sampleMovement(player, session);
    } catch (e) {
      console.warn(`[ZP-AC] 移动采样 ${cleanText(player.name, 64)} 失败: ${e}`);
    }
  }

  for (const [playerId, session] of sessions.entries()) {
    if (!onlineIds.has(playerId) && tickCounter - session.lastSeenAt >= CONFIG.DISCONNECT_GRACE_TICKS) {
      sessions.delete(playerId);
      addHistory({
        type: "player_monitor_end",
        target: session.playerName,
        targetId: session.playerId,
        status: "offline",
        reportCount: session.reportCount
      });
    }
  }

  if (!isPluginOnline()) pluginModeAnnounced = false;
}

system.afterEvents.scriptEventReceive.subscribe(handleScriptEvent);
world.afterEvents.entityHitEntity.subscribe(trackHit);
world.afterEvents.playerSwingStart.subscribe(trackSwing);
world.afterEvents.playerPlaceBlock.subscribe(trackPlace);
system.runInterval(monitoringLoop, CONFIG.SAMPLE_INTERVAL);
system.runTimeout(loadHistory, 1);

console.warn("[ZP-AC] Bedrock 独立基础反作弊已启动：全员移动/战斗/挥臂/搭路行为观察；无 Endstone 插件时仍可独立告警与保存历史。");


/* ===== Integrated /ba player report UI (same behavior pack) ===== */

/*
 * ZP 玩家举报系统 - Endstone 联动版
 * Minecraft Bedrock 1.26.60 / @minecraft/server 2.10.0
 * 指令：/ba
 * 行为基础检测由独立 ZP-AC Behavior Pack 执行；Endstone 插件仅提供增强评分/处罚。
 */

const REPORT_UI_CONFIG = {
    command: "zp_ac:ba",
    title: "§l§6玩家举报系统",
    successMessage: "§a举报已提交；独立基础反作弊持续运行，若安装增强插件则同步证据。",
    maxReasonLength: 200
};

let reportSequence = 0;

function sendMessage(player, message) {
    try { player.sendMessage(message); }
    catch (error) { console.warn(`[PlayerReport] 消息发送失败: ${error}`); }
}

function getAvailableTargets(player) {
    return world.getPlayers().filter(target => target.id !== player.id);
}

function encodeBase64Utf8(value) {
    // 先把 encodeURIComponent 产生的 UTF-8 百分号序列还原为字节，再做 Base64。
    const escaped = encodeURIComponent(value);
    const bytes = [];
    for (let i = 0; i < escaped.length; i += 1) {
        if (escaped[i] === "%") {
            bytes.push(parseInt(escaped.slice(i + 1, i + 3), 16));
            i += 2;
        } else {
            bytes.push(escaped.charCodeAt(i));
        }
    }
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let output = "";
    for (let i = 0; i < bytes.length; i += 3) {
        const a = bytes[i];
        const hasB = i + 1 < bytes.length;
        const hasC = i + 2 < bytes.length;
        const b = hasB ? bytes[i + 1] : 0;
        const c = hasC ? bytes[i + 2] : 0;
        output += alphabet[(a >> 2) & 63];
        output += alphabet[((a & 3) << 4) | (b >> 4)];
        output += hasB ? alphabet[((b & 15) << 2) | (c >> 6)] : "=";
        output += hasC ? alphabet[c & 63] : "=";
    }
    return output;
}

function submitReportToEndstone(reporter, target, reason, reportId) {
    const payload = {
        version: "zp-report-v1",
        reportId,
        reporterName: reporter.name,
        reporterId: reporter.id,
        targetName: target.name,
        targetId: target.id,
        reason
    };
    const encoded = encodeBase64Utf8(JSON.stringify(payload));
    // Base64 压缩 UTF-8 内容，降低中文理由导致 scriptevent 超过 2048 字符上限的风险。
    // scriptevent 由 Script API 的服务器维度命令上下文执行，普通玩家无需 OP。
    const result = world.getDimension("overworld").runCommand(`scriptevent zp:report_submitted b64:${encoded}`);
    if (result && result.successCount === 0) {
        throw new Error("scriptevent 命令未成功执行");
    }
}

function openReportMenu(player) {
    const targets = getAvailableTargets(player);
    if (targets.length === 0) {
        sendMessage(player, "§c当前没有其他在线玩家可以举报。");
        return;
    }

    const targetIds = targets.map(target => target.id);
    const targetNames = targets.map(target => target.name);
    const form = new ModalFormData()
        .title(REPORT_UI_CONFIG.title)
        .dropdown("§l§f选择举报对象", targetNames)
        .textField("§l§f举报理由", "请输入举报理由...", { defaultValue: "" })
        .submitButton("§l§a提交举报");

    form.show(player).then(response => {
        if (response.canceled) return;
        const values = response.formValues;
        if (!values || values.length < 2) {
            sendMessage(player, "§c提交失败，请重新打开举报菜单。");
            return;
        }

        const selectedIndex = values[0];
        const rawReason = values[1];
        if (typeof selectedIndex !== "number" || !Number.isInteger(selectedIndex) ||
            selectedIndex < 0 || selectedIndex >= targetIds.length) {
            sendMessage(player, "§c请选择有效的举报对象。");
            return;
        }
        if (typeof rawReason !== "string") {
            sendMessage(player, "§c举报理由无效。");
            return;
        }
        const reason = rawReason.trim();
        if (!reason) {
            sendMessage(player, "§c举报理由不能为空。");
            return;
        }
        if (reason.length > REPORT_UI_CONFIG.maxReasonLength) {
            sendMessage(player, "§c举报理由最多200个字符。");
            return;
        }

        const targetId = targetIds[selectedIndex];
        const target = world.getPlayers().find(p => p.id === targetId && p.id !== player.id);
        if (!target) {
            sendMessage(player, "§c该玩家已经离线，请重新选择。");
            return;
        }

        reportSequence += 1;
        const reportId = `${Date.now()}-${reportSequence}`;
        try {
            submitReportToEndstone(player, target, reason, reportId);
            sendMessage(player, REPORT_UI_CONFIG.successMessage);
        } catch (error) {
            console.warn(`[PlayerReport] 向 Endstone 发送举报失败: ${error}`);
            sendMessage(player, "§c举报提交失败：服务器暂时无法接收举报信息；独立基础检测仍持续运行。");
        }
    }).catch(error => {
        console.warn(`[PlayerReport] UI错误: ${error}`);
        sendMessage(player, "§c举报菜单发生错误，请稍后重试。");
    });
}


/* ===== ZP-AC administrator panel: native Bedrock server forms ===== */
const ADMIN_UI_COMMAND = "zp_ac:admin";
const ADMIN_TAGS_UI = ["zp_ac_admin", "zp:ac_admin", CONFIG.OP_ADMIN_TAG];
const adminRequests = new Map();
let adminRequestSequence = 0;

function isUiAdmin(player) {
  try {
    return ADMIN_TAGS_UI.some((tag) => player.hasTag(tag));
  } catch {
    return false;
  }
}

function uiAdminMessage(player, message) {
  try { player.sendMessage(`§8[§cZP-AC 管理§8] §r${message}`); } catch { /* player left */ }
}

function showAdminPanel(player) {
  if (!isUiAdmin(player)) {
    uiAdminMessage(player, "没有管理权限。请由服务器管理员为你添加标签：/tag @s add zp_ac_admin，然后重新输入 /zp_ac:admin。");
    return;
  }

  const online = world.getPlayers().filter((p) => !isUiAdmin(p));
  const pluginOnline = isPluginOnline();
  const form = new ActionFormData()
    .title("§l§cZP-AC 管理控制台")
    .body(
      "§7独立反作弊 + Endstone 增强层\n\n" +
      "§f运行模式：§a" + (pluginOnline ? "插件增强模式" : "行为包独立模式") + "\n" +
      "§f在线玩家：§e" + world.getPlayers().length + "\n" +
      "§f监控会话：§e" + sessions.size + "\n" +
      "§f本地历史：§e" + history.length + " 条\n\n" +
      "§8本界面按需打开，不使用常驻 HUD。"
    )
    .button("§l系统状态")
    .button("§l在线玩家监控")
    .button("§l最近检测记录")
    .button("§l增强插件管理")
    .button("§c关闭");

  form.show(player).then((response) => {
    if (response.canceled) return;
    switch (response.selection) {
      case 0:
        showAdminStatus(player);
        break;
      case 1:
        showAdminPlayers(player);
        break;
      case 2:
        showAdminHistory(player);
        break;
      case 3:
        showPluginAdminPanel(player);
        break;
      default:
        break;
    }
  }).catch((error) => {
    console.warn("[ZP-AC] 管理主界面打开失败: " + error);
    uiAdminMessage(player, "界面暂时无法打开，请稍后重试。");
  });
}

function showAdminStatus(player) {
  if (!isUiAdmin(player)) return;
  const body =
    "§l§f运行状态\n" +
    "§f行为包版本：§e2.1.2\n" +
    "§f插件桥接：§" + (isPluginOnline() ? "a已连接" : "e未连接/独立运行") + "\n" +
    "§f在线玩家：§e" + world.getPlayers().length + "\n" +
    "§f活跃监控会话：§e" + sessions.size + "\n" +
    "§f历史证据：§e" + history.length + "\n" +
    "§f插件租约剩余：§e" + Math.max(0, Math.ceil((pluginLeaseUntil - tickCounter) / 20)) + " 秒\n\n" +
    "§7插件未连接时，行为包基础检测和本地历史仍继续运行。";
  new MessageFormData()
    .title("§l系统状态")
    .body(body)
    .button1("返回控制台")
    .button2("关闭")
    .show(player)
    .then((response) => {
      if (!response.canceled && response.selection === 0) showAdminPanel(player);
    })
    .catch((error) => console.warn("[ZP-AC] 状态界面失败: " + error));
}

function showAdminPlayers(player) {
  if (!isUiAdmin(player)) return;
  const players = world.getPlayers().filter((p) => !isUiAdmin(p));
  if (!players.length) {
    uiAdminMessage(player, "当前没有可查看的普通在线玩家。");
    return;
  }
  const form = new ActionFormData().title("§l在线玩家监控").body("§7选择玩家查看行为包侧监控摘要。");
  for (const target of players.slice(0, 40)) {
    const session = sessions.get(target.id);
    const status = session ? "§a监控中" : "§7等待采样";
    form.button(target.name + "\n" + status);
  }
  form.button("§c返回");
  form.show(player).then((response) => {
    if (response.canceled || response.selection === undefined) return;
    if (response.selection >= Math.min(players.length, 40)) {
      showAdminPanel(player);
      return;
    }
    const target = players[response.selection];
    if (target) showAdminPlayerDetail(player, target.id);
  }).catch((error) => console.warn("[ZP-AC] 玩家列表界面失败: " + error));
}

function showAdminPlayerDetail(admin, targetId) {
  if (!isUiAdmin(admin)) return;
  const target = world.getPlayers().find((p) => p.id === targetId);
  if (!target) {
    uiAdminMessage(admin, "该玩家已离线。");
    return;
  }
  const session = sessions.get(target.id);
  const body = session
    ? "§f玩家：§e" + cleanText(target.name, 64) + "\n" +
      "§f状态：§a基础行为监控中\n" +
      "§f累计举报：§e" + session.reportCount + "\n" +
      "§f最近证据时间：§e" + Math.max(0, Math.floor((tickCounter - session.lastEvidenceAt) / 20)) + " 秒前\n" +
      "§f已记录原因：§e" + (session.reasons.length ? session.reasons.join("、") : "无") + "\n" +
      "§f最近位置：§e" + coords(target) + "\n\n" +
      "§7此处只显示行为包本地信息；插件隐藏画像不会发送到客户端。"
    : "§f玩家：§e" + cleanText(target.name, 64) + "\n§7当前尚无独立行为包会话摘要。";
  new MessageFormData()
    .title("§l玩家监控摘要")
    .body(body)
    .button1("返回玩家列表")
    .button2("关闭")
    .show(admin)
    .then((response) => {
      if (!response.canceled && response.selection === 0) showAdminPlayers(admin);
    })
    .catch((error) => console.warn("[ZP-AC] 玩家详情界面失败: " + error));
}

function showAdminHistory(player) {
  if (!isUiAdmin(player)) return;
  const entries = history.slice(-12).reverse();
  if (!entries.length) {
    uiAdminMessage(player, "当前没有本地历史记录。");
    return;
  }
  const form = new ActionFormData().title("§l最近检测记录").body("§7最近 12 条本地记录，选择一条查看详情。");
  for (const entry of entries) {
    const label = cleanText(entry.target || entry.type || "记录", 48);
    const kind = cleanText(entry.title || entry.code || entry.type || "事件", 48);
    form.button(label + "\n§8" + kind);
  }
  form.button("§c返回");
  form.show(player).then((response) => {
    if (response.canceled || response.selection === undefined) return;
    if (response.selection >= entries.length) {
      showAdminPanel(player);
      return;
    }
    const entry = entries[response.selection];
    const detail = JSON.stringify(entry, null, 2);
    new MessageFormData()
      .title("§l记录详情")
      .body("§7" + detail.slice(0, 3500))
      .button1("返回记录")
      .button2("关闭")
      .show(player)
      .then((next) => {
        if (!next.canceled && next.selection === 0) showAdminHistory(player);
      });
  }).catch((error) => console.warn("[ZP-AC] 历史记录界面失败: " + error));
}


/* ===== Endstone management bridge: native, on-demand admin forms ===== */
function requestPluginAdmin(player, action, values = {}, callback = () => {}) {
  if (!isUiAdmin(player)) {
    uiAdminMessage(player, "没有管理权限。");
    return;
  }
  if (!isPluginOnline()) {
    uiAdminMessage(player, "Endstone 增强插件当前未连接；行为包基础检测仍然运行。");
    return;
  }
  const requestId = `${Date.now()}-${++adminRequestSequence}`;
  const payload = {
    version: CONFIG.ADMIN_PROTOCOL,
    requestId,
    adminName: cleanText(player.name, 64),
    action,
    ...values
  };
  adminRequests.set(requestId, {
    adminName: cleanText(player.name, 64).toLocaleLowerCase(),
    callback
  });
  if (!runServerEvent(CONFIG.ADMIN_REQUEST_EVENT, JSON.stringify(payload))) {
    adminRequests.delete(requestId);
    uiAdminMessage(player, "管理请求发送失败，请稍后重试。");
    return;
  }
  system.runTimeout(() => {
    if (!adminRequests.has(requestId)) return;
    adminRequests.delete(requestId);
    const stillOnline = world.getPlayers().some(
      (p) => p.name.toLocaleLowerCase() === cleanText(player.name, 64).toLocaleLowerCase()
    );
    if (stillOnline) uiAdminMessage(player, "管理请求超时；请检查增强插件和 ScriptEvent 通信。");
  }, 20 * 12);
}

function handleAdminResponse(event) {
  if (event.sourceType !== ScriptEventSource.Server) return;
  let response;
  try {
    const raw = String(event.message || "");
    response = JSON.parse(raw.startsWith("b64:")
      ? decodeBase64Utf8(raw.slice(4))
      : decodeURIComponent(raw));
  } catch (error) {
    console.warn("[ZP-AC] 管理响应解析失败: " + error);
    return;
  }
  if (!response || response.version !== CONFIG.ADMIN_PROTOCOL) return;
  const requestId = String(response.requestId || "");
  const pending = adminRequests.get(requestId);
  if (!pending) return;
  if (String(response.adminName || "").toLocaleLowerCase() !== pending.adminName) return;
  adminRequests.delete(requestId);
  const player = world.getPlayers().find(
    (p) => p.name.toLocaleLowerCase() === pending.adminName
  );
  if (!player || !isUiAdmin(player)) return;
  try {
    pending.callback(player, response);
  } catch (error) {
    console.warn("[ZP-AC] 管理界面回调失败: " + error);
    uiAdminMessage(player, "管理界面处理响应时发生错误。");
  }
}

function showPluginAdminPanel(player) {
  requestPluginAdmin(player, "overview", {}, (admin, response) => {
    if (!response.ok) {
      uiAdminMessage(admin, response.error || "无法读取插件状态。");
      return;
    }
    const data = response.data || {};
    const form = new ActionFormData()
      .title("§l§cZP-AC 增强插件管理")
      .body(
        "§f插件版本：§e" + cleanText(data.plugin_version || "未知", 24) + "\n" +
        "§f监控会话：§e" + Number(data.active_sessions || 0) + "\n" +
        "§f待复核候选：§e" + Number(data.candidate_count || 0) + "\n" +
        "§f本地永久封禁 UUID：§e" + Number(data.local_ban_count || 0) + "\n" +
        "§f隐藏画像数量：§e" + Number(data.profile_count || 0) + "\n" +
        "§f云端同步：§" + (data.cloud_enabled ? "a已启用" : "7已关闭") + "\n\n" +
        "§8风险分与可信分不会向普通玩家公开。"
      )
      .button("§l待复核候选")
      .button("§l查看隐藏可信画像")
      .button("§l本地封禁名单 / 撤销封禁")
      .button("§l云端 UUID 同步")
      .button("§c返回控制台");
    form.show(admin).then((result) => {
      if (result.canceled) return;
      switch (result.selection) {
        case 0: showPluginCandidates(admin); break;
        case 1: showPluginTrustPrompt(admin); break;
        case 2: showPluginBanList(admin); break;
        case 3: showPluginCloudPanel(admin); break;
        case 4: showAdminPanel(admin); break;
      }
    }).catch((error) => {
      console.warn("[ZP-AC] 增强插件管理界面失败: " + error);
      uiAdminMessage(admin, "无法打开增强插件管理界面。");
    });
  });
}

function showPluginCandidates(player) {
  requestPluginAdmin(player, "candidate_list", {}, (admin, response) => {
    if (!response.ok) {
      uiAdminMessage(admin, response.error || "读取候选列表失败。");
      return;
    }
    const candidates = Array.isArray(response.data?.candidates) ? response.data.candidates : [];
    if (!candidates.length) {
      uiAdminMessage(admin, "当前没有达到严重行为门槛的待复核候选。");
      showPluginAdminPanel(admin);
      return;
    }
    const form = new ActionFormData()
      .title("§l待复核严重行为")
      .body("§7选择候选查看依据。永久封禁仍必须再次确认。");
    for (const candidate of candidates) {
      const name = cleanText(candidate.player_name || "未知玩家", 24);
      const rules = Array.isArray(candidate.rules) ? candidate.rules.slice(0, 3).join(",") : "";
      form.button(name + "\n§c嫌疑分 " + Number(candidate.score || 0) +
        " §7| §f" + cleanText(rules, 48));
    }
    form.button("§c返回");
    form.show(admin).then((result) => {
      if (result.canceled || result.selection === undefined) return;
      if (result.selection >= candidates.length) {
        showPluginAdminPanel(admin);
        return;
      }
      const candidate = candidates[result.selection];
      const uuid = String(candidate.uuid || "");
      new MessageFormData()
        .title("§l候选复核")
        .body(
          "§f玩家：§e" + cleanText(candidate.player_name || "未知", 32) + "\n" +
          "§fUUID：§7" + uuid + "\n" +
          "§f会话嫌疑分：§c" + Number(candidate.score || 0) + "\n" +
          "§f独立举报者：§e" + Number(candidate.reporter_count || 0) + "\n" +
          "§f规则：§e" + cleanText((candidate.rules || []).join("、"), 160) + "\n\n" +
          "§c确认后将写入本地永久 UUID 封禁名单，并踢出在线目标。"
        )
        .button1("确认永久封禁")
        .button2("取消")
        .show(admin)
        .then((confirm) => {
          if (confirm.canceled || confirm.selection !== 0) return;
          requestPluginAdmin(admin, "confirmban", { uuid }, (currentAdmin, resultResponse) => {
            uiAdminMessage(currentAdmin, resultResponse.ok
              ? "已完成管理员确认并写入永久 UUID 封禁名单。"
              : (resultResponse.error || "封禁确认失败。"));
            showPluginAdminPanel(currentAdmin);
          });
        });
    }).catch((error) => {
      console.warn("[ZP-AC] 候选列表界面失败: " + error);
      uiAdminMessage(admin, "无法显示候选列表。");
    });
  });
}

function showPluginTrustPrompt(player) {
  const form = new ModalFormData()
    .title("§l查询隐藏玩家画像")
    .textField("输入在线玩家名或 UUID", "玩家名 / UUID", { defaultValue: "" })
    .submitButton("查询画像");
  form.show(player).then((result) => {
    if (result.canceled) return;
    const value = String(result.formValues?.[0] || "").trim();
    if (!value) {
      uiAdminMessage(player, "请输入玩家名或 UUID。");
      return;
    }
    requestPluginAdmin(player, "trust", { value }, (admin, response) => {
      if (!response.ok) {
        uiAdminMessage(admin, response.error || "画像查询失败。");
        return;
      }
      const profile = response.data || {};
      new MessageFormData()
        .title("§l隐藏玩家画像")
        .body(
          "§fUUID：§7" + cleanText(profile.uuid || "", 64) + "\n" +
          "§f隐藏可信分：§a" + Number(profile.trust ?? 50).toFixed(1) + "/100\n" +
          "§f风险分：§c" + Number(profile.risk ?? 0).toFixed(1) + "/100\n" +
          "§f累计证据：§e" + Number(profile.flags || 0) + "\n" +
          "§f强证据：§e" + Number(profile.strong_flags || 0) + "\n" +
          "§f累计活跃监控：§e" + Math.floor(Number(profile.active_seconds || 0) / 60) + " 分钟\n" +
          "§f最近规则：§e" + cleanText(profile.last_rule || "无", 48) + "\n\n" +
          "§7分数只供管理员复核，不代表作弊概率。"
        )
        .button1("返回")
        .button2("关闭")
        .show(admin)
        .then((next) => {
          if (!next.canceled && next.selection === 0) showPluginAdminPanel(admin);
        });
    });
  }).catch((error) => {
    console.warn("[ZP-AC] 画像查询表单失败: " + error);
    uiAdminMessage(player, "无法打开画像查询表单。");
  });
}

function showPluginBanList(player) {
  requestPluginAdmin(player, "bans", {}, (admin, response) => {
    if (!response.ok) {
      uiAdminMessage(admin, response.error || "读取封禁名单失败。");
      return;
    }
    const uuids = Array.isArray(response.data?.uuids) ? response.data.uuids : [];
    if (!uuids.length) {
      uiAdminMessage(admin, "本地永久封禁名单为空。");
      showPluginAdminPanel(admin);
      return;
    }
    const form = new ActionFormData()
      .title("§l本地永久封禁 UUID")
      .body("§7选择 UUID 后可发起撤销操作。");
    for (const uuid of uuids) form.button(String(uuid));
    form.button("§c返回");
    form.show(admin).then((result) => {
      if (result.canceled || result.selection === undefined) return;
      if (result.selection >= uuids.length) {
        showPluginAdminPanel(admin);
        return;
      }
      const uuid = String(uuids[result.selection]);
      new MessageFormData()
        .title("§l撤销 UUID 封禁")
        .body("确定撤销该 UUID 的本地封禁，并将撤销状态加入同步队列吗？\n\n§7" + uuid)
        .button1("确认撤销")
        .button2("取消")
        .show(admin)
        .then((confirm) => {
          if (confirm.canceled || confirm.selection !== 0) return;
          requestPluginAdmin(admin, "pardon", { uuid }, (currentAdmin, resultResponse) => {
            uiAdminMessage(currentAdmin, resultResponse.ok
              ? "已撤销该 UUID 的封禁，并重置内部画像。"
              : (resultResponse.error || "撤销封禁失败。"));
            showPluginAdminPanel(currentAdmin);
          });
        });
    });
  });
}

function showPluginCloudPanel(player) {
  requestPluginAdmin(player, "cloud_status", {}, (admin, response) => {
    if (!response.ok) {
      uiAdminMessage(admin, response.error || "读取云端状态失败。");
      return;
    }
    const data = response.data || {};
    const enabled = Boolean(data.enabled);
    const form = new ActionFormData()
      .title("§l云端 UUID 封禁同步")
      .body(
        "§f状态：§" + (enabled ? "a已启用" : "7已关闭") + "\n" +
        "§f读取令牌：§e" + (data.read_token_configured ? "已配置" : "未配置") + "\n" +
        "§f写入令牌：§e" + (data.write_token_configured ? "已配置" : "未配置") + "\n" +
        "§f本地 UUID：§e" + Number(data.local_ban_count || 0) + "\n" +
        "§f云端缓存 UUID：§e" + Number(data.cloud_ban_count || 0) + "\n" +
        (data.problem ? "§c配置问题：" + cleanText(data.problem, 180) : "§7云端只同步 UUID，不上传玩家名称或证据。")
      )
      .button(enabled ? "§c关闭云端同步" : "§a启用云端同步")
      .button("立即同步")
      .button("返回");
    form.show(admin).then((result) => {
      if (result.canceled || result.selection === undefined) return;
      if (result.selection === 2) {
        showPluginAdminPanel(admin);
        return;
      }
      const action = result.selection === 0 ? (enabled ? "cloud_off" : "cloud_on") : "cloud_sync";
      requestPluginAdmin(admin, action, {}, (currentAdmin, resultResponse) => {
        uiAdminMessage(currentAdmin, resultResponse.ok
          ? (resultResponse.data?.message || "云端操作已提交。")
          : (resultResponse.error || "云端操作失败。"));
        showPluginCloudPanel(currentAdmin);
      });
    });
  });
}


system.beforeEvents.startup.subscribe((event) => {
  const registrations = [
    {
      name: REPORT_UI_CONFIG.command,
      description: "打开玩家举报系统",
      permissionLevel: CommandPermissionLevel.Any,
      cheatsRequired: false
    },
    {
      name: ADMIN_UI_COMMAND,
      description: "打开 ZP-AC 管理员控制台",
      permissionLevel: CommandPermissionLevel.Any,
      cheatsRequired: false
    }
  ];

  const handlers = {
    [REPORT_UI_CONFIG.command]: (player) => openReportMenu(player),
    [ADMIN_UI_COMMAND]: (player) => showAdminPanel(player)
  };

  for (const definition of registrations) {
    try {
      event.customCommandRegistry.registerCommand(definition, (origin) => {
        const player = origin.initiator ?? origin.sourceEntity;
        if (!(player instanceof Player)) {
          return {
            status: CustomCommandStatus.Failure,
            message: "此指令只能由玩家执行。"
          };
        }
        system.run(() => handlers[definition.name](player));
        return { status: CustomCommandStatus.Success };
      });
      console.warn("[ZP-AC] 自定义命令注册成功: /" + definition.name);
    } catch (error) {
      console.error("[ZP-AC] 自定义命令注册失败: /" + definition.name + "；" + error);
    }
  }

  console.warn("[ZP-AC] 启动完成；命令命名空间统一为 zp_ac；举报与管理员 UI 分别受各自权限校验。");
});
