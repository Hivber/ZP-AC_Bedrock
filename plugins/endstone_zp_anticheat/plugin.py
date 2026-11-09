import base64
import json
import math
import time
import queue
import threading
import uuid as uuidlib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from typing import Any

from endstone.command import Command, CommandSender, ConsoleCommandSender

from endstone.event import (
    ActorDamageEvent,
    BlockBreakEvent,
    BlockPlaceEvent,
    PacketReceiveEvent,
    PlayerDimensionChangeEvent,
    PlayerGameModeChangeEvent,
    PlayerInteractEvent,
    PlayerJoinEvent,
    PlayerLoginEvent,
    PlayerMoveEvent,
    PlayerQuitEvent,
    PlayerRespawnEvent,
    PlayerTeleportEvent,
    ScriptMessageEvent,
    event_handler,
)
from endstone.plugin import Plugin

from .ai.engine import LocalBehaviorAI
from .ai.dataset import BehaviorDatasetCollector
from .ai.onnx_engine import OnnxBehaviorModel
from .movement import read_effect_amplifier, speed_effect_multiplier
from .policy import has_independent_strong_evidence, profile_penalty
from .credit import PUBLIC_CREDIT_DEFAULT, PUBLIC_CREDIT_LOCK_THRESHOLD, accrue_public_credit, can_damage_players, clamp_public_credit, is_retaliation_allowed, should_count_pvp_hit


MESSAGE_ID = "zp:report_submitted"
REPORT_VERSION = "zp-report-v1"
BP_READY_ID = "zp:ac_plugin_ready"
BP_EVIDENCE_ID = "zp:ac_bp_evidence"
BP_ADMIN_REQUEST_ID = "zp:ac_admin_request"
BP_ADMIN_RESPONSE_ID = "zp:ac_admin_response"
BP_ADMIN_PROTOCOL = "zp-ac-admin-v1"
BP_CHECKS_SYNC_ID = "zp:ac_checks_sync"
BP_CHECKS_PROTOCOL = "zp-ac-checks-v1"
BP_OP_ADMIN_TAG = "zp_ac_op_admin"
BP_PROTOCOL = "zp-ac-bp-v1"
BP_RULE_MAP = {
    "speed": ("speed_sustained", False),
    "hover": ("hover_confirmed", True),
    "ascending": ("hover_confirmed", False),
    "reach_hard": ("reach_repeated", False),
    "reach_repeat": ("reach_repeated", False),
    "autoclick": ("autoclick_pattern", False),
    "multi_target": ("multi_target_combat", False),
    "scaffold": ("scaffold_pattern", False),
}
DEFAULT_ANTI_CHEAT_CHECKS = {
    "speed": True, "flight": True, "reach": True, "combat": True,
    "autoclick": True, "scaffold": True, "fast_break": True, "xray": True,
}
RULE_CHECK_GROUP = {
    "speed_sustained": "speed", "hover_confirmed": "flight",
    "reach_extreme": "reach", "reach_repeated": "reach",
    "multi_target_combat": "combat", "rapid_block_break": "fast_break",
    "xray_mining_pattern": "xray",
    "autoclick_pattern": "autoclick", "scaffold_pattern": "scaffold",
}
ADMIN_TAGS = {"zp_ac_admin", "zp:ac_admin", BP_OP_ADMIN_TAG}
BYPASS_TAGS = {"zp_ac_bypass", "zp:ac_bypass"}

RULE_TITLES = {
    "speed_sustained": "持续异常高速移动",
    "hover_confirmed": "持续空中悬浮/疑似飞行",
    "reach_extreme": "多次异常超距近战攻击",
    "reach_repeated": "重复出现超出正常范围的近战命中",
    "multi_target_combat": "短时间内异常连续攻击多个玩家",
    "rapid_block_break": "短时间内异常密集破坏方块（辅助线索）",
    "xray_mining_pattern": "异常矿物定向开采模式（透视辅助线索）",
    "autoclick_pattern": "异常规律的高频左键操作（辅助线索）",
    "scaffold_pattern": "移动中连续在脚下/身侧放置方块（疑似自动搭路，辅助线索）",
}

BASE_POINTS = {
    "speed_sustained": 4,
    "hover_confirmed": 7,
    "reach_extreme": 8,
    "reach_repeated": 4,
    "multi_target_combat": 7,
    "rapid_block_break": 3,
    "xray_mining_pattern": 3,
    "autoclick_pattern": 2,
    "scaffold_pattern": 2,
}

STRONG_RULES = {
    "speed_sustained",
    "hover_confirmed",
    "reach_extreme",
    "multi_target_combat",
}


def _clean(value: Any, limit: int = 180) -> str:
    text = str(value or "")
    text = " ".join("".join(ch if ch >= " " else " " for ch in text).split())
    return text[:limit]


def _player_key(player: Any) -> str | None:
    try:
        value = getattr(player, "unique_id", None)
        if value is None:
            return None
        try:
            return str(uuidlib.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            return str(value)
    except Exception:
        return None


def _player_name(player: Any) -> str:
    try:
        return _clean(getattr(player, "name", ""), 64)
    except Exception:
        return ""


def _xyz(value: Any) -> tuple[float, float, float] | None:
    try:
        return float(value.x), float(value.y), float(value.z)
    except Exception:
        return None


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _horizontal_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.hypot(a[0] - b[0], a[2] - b[2])


def _mode_text(player: Any) -> str:
    try:
        mode = getattr(player, "game_mode", "")
        return str(getattr(mode, "name", mode)).lower()
    except Exception:
        return ""


class ZPReportAntiCheat(Plugin):
    """Endstone enhancement layer for the standalone ZP-AC behavior pack."""

    api_version = "0.11"
    name = "ZPReportAntiCheat"
    prefix = "ZP-AC"
    description = "ZP-AC增强层：隐藏可信画像、延迟感知检测、证据关联与UUID封禁复核"
    authors = ["ZP Server Tools"]
    version = "0.6.9"
    commands = {
        "zp": {"description": "查看自己的 ZP 公开信用分。", "usages": ["/zp"], "permissions": ["zp_report_anticheat.credit"]},
        "zpac": {
            "description": "管理 ZP 反作弊与云端 UUID 封禁同步。",
            "usages": [
                "/zpac <action: str> [value: message]",
            ],
            "permissions": ["zp_report_anticheat.admin"],
        }
    }
    permissions = {
        "zp_report_anticheat.admin": {
            "description": "允许管理 ZP 反作弊、云端同步及确认永久封禁。",
            "default": True,
        },
        "zp_report_anticheat.credit": {
            "description": "允许玩家使用 /zp 查看自己的公开信用分。",
            "default": True,
        },
    }

    def on_enable(self) -> None:
        self.save_default_config()
        defaults = {
            "behavior_pack_bridge_enabled": True,
            "anti_cheat_checks": dict(DEFAULT_ANTI_CHEAT_CHECKS),
            "cloud_enabled": False,
            "cloud_endpoint": "",
            "cloud_read_token": "",
            "cloud_write_token": "",
            "cloud_sync_interval_seconds": 60,
            "cloud_timeout_seconds": 4,
            "trust_enabled": True,
            "trust_initial_score": 50.0,
            "public_credit_enabled": True,
            "public_credit_initial_score": 100.0,
            "public_credit_recovery_per_hour": 0.5,
            "public_credit_retaliation_seconds": 5.0,
            "public_credit_lock_threshold": 70.0,
            "public_credit_cheat_penalty": 12.0,
            "public_credit_assault_window_seconds": 20.0,
            "public_credit_assault_hits_required": 8,
            "public_credit_assault_penalty": 5.0,
            "public_credit_assault_cooldown_seconds": 600.0,
            "trust_recovery_per_hour": 0.25,
            "trust_recovery_delay_seconds": 3600,
            "risk_decay_per_hour": 0.20,
            "risk_decay_delay_seconds": 3600,
            "auto_kick_risk_threshold": 22.0,
            "auto_kick_min_strong_rules": 2,
            "profile_save_interval_seconds": 60,
            "profile_limit": 10000,
            "ping_speed_threshold_start": 150,
            "ping_speed_threshold_scale": 0.00045,
            "ping_speed_window_bonus": 1,
            "reach_ping_allowance_scale": 0.0010,
            "reach_ping_allowance_max": 0.90,
            "xray_window_seconds": 180.0,
            "xray_min_rock_blocks": 40,
            "xray_min_ore_blocks": 5,
            "xray_min_rare_ores": 2,
            "xray_min_clusters": 3,
            "xray_min_ore_ratio": 0.08,
            "xray_cluster_distance": 5.5,
            "ai_enabled": True,
            "ai_window_seconds": 120.0,
            "ai_analysis_interval_seconds": 5.0,
            "ai_min_baseline_samples": 12,
            "ai_anomaly_threshold": 3.5,
            "ai_min_anomalous_features": 2,
            "ai_api_enabled": False,
            "ai_api_key": "",
            "ai_api_endpoint": "",
        }
        changed = False
        for key, value in defaults.items():
            if key not in self.config:
                self.config[key] = value
                changed = True
        if changed:
            self.save_config()
        self.cfg = dict(self.config)

        self.sessions: dict[str, dict[str, Any]] = {}
        self.ai_engine = LocalBehaviorAI(window_seconds=float(self.cfg.get("ai_window_seconds", 120.0)), min_samples=int(self.cfg.get("ai_min_baseline_samples", 12)), threshold=float(self.cfg.get("ai_anomaly_threshold", 3.5)), min_features=int(self.cfg.get("ai_min_anomalous_features", 2)), max_players=max(100, int(self.cfg.get("profile_limit", 10000))))
        self.ai_model = OnnxBehaviorModel(
            self.cfg.get("ai_onnx_model_path") or (Path(self.data_folder) / "models" / "zp_ac_behavior.onnx"),
            enabled=bool(self.cfg.get("ai_onnx_enabled", True)),
        )
        self.ai_dataset = BehaviorDatasetCollector(
            Path(self.data_folder) / "ai_dataset.jsonl",
            enabled=bool(self.cfg.get("ai_dataset_enabled", True)),
            interval_seconds=float(self.cfg.get("ai_dataset_interval_seconds", 30.0)),
            max_bytes=int(self.cfg.get("ai_dataset_max_bytes", 20000000)),
        )
        self._ai_last_analysis: dict[str, float] = {}
        self.report_cooldowns: dict[tuple[str, str], float] = {}
        self.packet_second: dict[str, dict[str, Any]] = {}
        self.break_events: dict[str, deque[float]] = defaultdict(deque)
        self.click_events: dict[str, deque[float]] = defaultdict(deque)
        self.pvp_recent_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.pvp_penalty_cooldowns: dict[tuple[str, str], float] = {}
        self.pvp_message_cooldowns: dict[str, float] = {}
        self.pvp_last_hit_at: dict[tuple[str, str], float] = {}
        self.audit_path = Path(self.data_folder) / "evidence.jsonl"
        self.data_path = Path(self.data_folder)
        self.data_path.mkdir(parents=True, exist_ok=True)

        # 永久封禁表、云端缓存和撤销队列仅包含 UUID，不含玩家名。
        self.local_bans_path = self.data_path / "permanent_ban_uuids.json"
        self.cloud_cache_path = self.data_path / "cloud_ban_cache_uuids.json"
        self.revocations_path = self.data_path / "cloud_revocations_uuids.json"
        # 待复核材料只保存在本地；云 API 永远不会接收这些名称或证据详情。
        self.candidates_path = self.data_path / "ban_review_candidates.json"
        self.profiles_path = self.data_path / "player_profiles.json"
        self.player_profiles = self._load_profiles()
        self._profiles_dirty = False
        self._profiles_last_save = time.monotonic()
        self.local_permanent_bans = self._load_uuid_set(self.local_bans_path)
        self.cloud_ban_cache = self._load_uuid_set(self.cloud_cache_path)
        self.revoked_uuids = self._load_uuid_set(self.revocations_path)
        self.ban_candidates = self._load_json_object(self.candidates_path)

        self._state_lock = threading.RLock()
        self._cloud_sync_lock = threading.Lock()
        self._cloud_check_lock = threading.Lock()
        self._cloud_checking: set[str] = set()
        self._cloud_results: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._cloud_last_sync = 0.0
        self._closing = False
        self._bp_last_ready = 0.0
        self._admin_request_last: dict[str, float] = {}

        self.register_events(self)
        self._pulse_task = self.server.scheduler.run_task(
            self, self._pulse, delay=5, period=5
        )
        self.logger.info(
            f"ZP-AC增强层已加载 v{self.version}；行为包桥接={self.cfg.get('behavior_pack_bridge_enabled', True)}；"
            f"自动踢出={self.cfg.get('auto_kick', True)}；隐藏可信画像={'启用' if self.cfg.get('trust_enabled', True) else '关闭'}；"
            "永久封禁必须管理员确认。"
        )
        self.logger.info(f"通信：举报 {MESSAGE_ID}；插件握手 {BP_READY_ID}；管理桥接 {BP_ADMIN_REQUEST_ID}/{BP_ADMIN_RESPONSE_ID}；云端默认关闭。")
        if bool(self.cfg.get("cloud_enabled", False)):
            self._start_cloud_sync("startup")

    def on_disable(self) -> None:
        self._closing = True
        try:
            self._pulse_task.cancel()
        except Exception:
            pass
        try:
            self._save_profiles()
        except Exception as exc:
            self.logger.warning(f"关闭时保存隐藏玩家画像失败：{type(exc).__name__}")
        self.logger.info("ZP 举报联动反作弊已卸载。")

    # ----------------------------- helpers -----------------------------

    def _log(self, kind: str, **fields: Any) -> None:
        row = {
            "time": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **fields,
        }
        try:
            if self.audit_path.exists() and self.audit_path.stat().st_size > 5 * 1024 * 1024:
                rotated = self.audit_path.with_suffix(".jsonl.1")
                try:
                    rotated.unlink(missing_ok=True)
                except Exception:
                    pass
                self.audit_path.replace(rotated)
            with self.audit_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as exc:
            self.logger.warning(f"写入反作弊证据日志失败: {exc}")

    def _is_admin_player(self, player: Any) -> bool:
        try:
            tags = set(getattr(player, "scoreboard_tags", []) or [])
            return bool(getattr(player, "is_op", False)) or bool(tags.intersection(ADMIN_TAGS))
        except Exception:
            return False

    def _is_admin_sender(self, sender: CommandSender) -> bool:
        if isinstance(sender, ConsoleCommandSender):
            return True
        target = self._find_online_player(_clean(getattr(sender, "name", ""), 64))
        return bool(target and self._is_admin_player(target))

    def _sync_operator_admin_tags(self) -> None:
        # Bedrock Script API cannot inspect OP status directly. Mirror OP status into
        # a reserved tag so the behavior-pack UI applies the same authorization rule.
        for player in list(self.server.online_players):
            try:
                tags = set(getattr(player, "scoreboard_tags", []) or [])
                is_op = bool(getattr(player, "is_op", False))
                if is_op and BP_OP_ADMIN_TAG not in tags:
                    player.add_scoreboard_tag(BP_OP_ADMIN_TAG)
                elif not is_op and BP_OP_ADMIN_TAG in tags:
                    player.remove_scoreboard_tag(BP_OP_ADMIN_TAG)
            except Exception:
                continue

    def _admins(self) -> list[Any]:
        return [player for player in list(self.server.online_players) if self._is_admin_player(player)]

    def _admin_alert(self, message: str) -> None:
        line = f"§8[§cZP反作弊§8] §r{message}"
        for admin in self._admins():
            try:
                admin.send_message(line)
            except Exception:
                pass
        self.logger.info(_clean(message, 500))

    def _is_exempt(self, player: Any) -> bool:
        try:
            tags = set(getattr(player, "scoreboard_tags", []) or [])
            if tags.intersection(BYPASS_TAGS | ADMIN_TAGS):
                return True
            if bool(getattr(player, "is_op", False)):
                return True
            mode = _mode_text(player)
            return "creative" in mode or "spectator" in mode
        except Exception:
            return False

    def _find_online_player(self, name: str) -> Any | None:
        wanted = name.casefold()
        for player in list(self.server.online_players):
            if _player_name(player).casefold() == wanted:
                return player
        return None

    def _tier(self, reporters: int) -> int:
        if reporters >= 5:
            return 4
        if reporters >= 3:
            return 3
        if reporters >= 2:
            return 2
        return 1

    def _tier_index(self, tier: int) -> int:
        return max(0, min(3, int(tier) - 1))

    def _tier_value(self, key: str, tier: int, default: list[float] | list[int]) -> float:
        values = self.cfg.get(key, default)
        if not isinstance(values, (list, tuple)) or not values:
            values = default
        idx = min(self._tier_index(tier), len(values) - 1)
        try:
            return float(values[idx])
        except Exception:
            return float(default[min(self._tier_index(tier), len(default) - 1)])

    def _anti_cheat_checks(self) -> dict[str, bool]:
        raw = self.cfg.get("anti_cheat_checks", {})
        if not isinstance(raw, dict):
            raw = {}
        return {key: raw.get(key, default) is not False for key, default in DEFAULT_ANTI_CHEAT_CHECKS.items()}

    def _check_enabled(self, key: str) -> bool:
        return self._anti_cheat_checks().get(key, True)

    def _store_anti_cheat_checks(self, values: Any) -> dict[str, bool]:
        if not isinstance(values, dict):
            raise ValueError("检测配置格式无效。")
        normalized = self._anti_cheat_checks()
        for key in DEFAULT_ANTI_CHEAT_CHECKS:
            if not isinstance(values.get(key), bool):
                raise ValueError(f"检测配置项 {key} 缺少有效布尔值。")
            normalized[key] = values[key]
        previous = self._anti_cheat_checks()
        if normalized == previous:
            return normalized
        self.cfg["anti_cheat_checks"] = dict(normalized)
        self.config["anti_cheat_checks"] = dict(normalized)
        try:
            self.save_config()
        except Exception:
            self.cfg["anti_cheat_checks"] = previous
            self.config["anti_cheat_checks"] = previous
            raise
        enabled = sum(1 for value in normalized.values() if value)
        self.logger.info(f"管理员更新反作弊检测开关：{enabled}/{len(normalized)} 项启用。")
        return normalized

    def _new_session(self, target: Any, reporter: Any, reason: str, report_id: str) -> dict[str, Any]:
        now = time.monotonic()
        key = _player_key(target)
        reporter_key = _player_key(reporter) or _player_name(reporter).casefold()
        loc = _xyz(getattr(target, "location", None)) or (0.0, 0.0, 0.0)
        return {
            "target_uuid": key,
            "target_name": _player_name(target),
            "started": now,
            "last_evidence": now,
            "last_seen": now,
            "offline_since": None,
            "reporters": {reporter_key: _player_name(reporter)},
            "reasons": [reason] if reason else [],
            "report_ids": [report_id] if report_id else [],
            "score": 0,
            "rule_codes": set(),
            "rule_counts": Counter(),
            "rule_last_at": {},
            "strong_codes": set(),
            "evidence": [],
            "last_position": loc,
            "last_dimension": self._dimension_key(target),
            "teleport_until": now + max(3.0, float(self.cfg.get("spawn_grace_seconds", 5.0))),
            "move_window_start": now,
            "move_window_distance": 0.0,
            "speed_windows": 0,
            "air_since": None,
            "air_start_y": None,
            "air_start_xz": None,
            "hover_flags": 0,
            "reach_soft": deque(),
            "reach_hard": deque(),
            "combat_hits": deque(),
            "packet_total": 0,
            "packet_counts": Counter(),
            "packet_totals": Counter(),
            "packet_payload_bytes": Counter(),
            "packet_payload_max": Counter(),
            "packet_second_start": int(now),
            "break_counted_until": 0.0,
            "scaffold_events": deque(),
            "xray_mining_events": deque(),
            "kicked": False,
            "autonomous": False,
        }

    def _ensure_autonomous_session(self, player: Any) -> None:
        if self._is_exempt(player):
            return
        key = _player_key(player)
        if not key or key in self.sessions:
            return
        session = self._new_session(player, player, "autonomous_server_monitor", "")
        session["reporters"].clear()
        session["reasons"] = ["autonomous_server_monitor"]
        session["autonomous"] = True
        self.sessions[key] = session
        self._log(
            "autonomous_monitor_start",
            target=_player_name(player),
            target_uuid=key,
            source="endstone_player_lifecycle",
        )

    def _dimension_key(self, player: Any) -> str:
        try:
            dimension = player.dimension
            return str(getattr(dimension, "name", getattr(dimension, "id", dimension)))
        except Exception:
            return "unknown"

    def _load_json_object(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _load_uuid_set(self, path: Path) -> set[str]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            values = value.get("uuids", []) if isinstance(value, dict) else value
            result = set()
            for item in values or []:
                try:
                    result.add(str(uuidlib.UUID(str(item))))
                except (ValueError, TypeError, AttributeError):
                    continue
            return result
        except Exception:
            return set()

    def _write_json_atomic(self, path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _save_uuid_set(self, path: Path, values: set[str]) -> None:
        cleaned = sorted({str(uuidlib.UUID(str(value))) for value in values})
        self._write_json_atomic(path, {"uuids": cleaned})

    def _load_profiles(self) -> dict[str, dict[str, Any]]:
        """Load UUID-keyed internal scores. Player names are deliberately not stored here."""
        try:
            raw = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            source = raw.get("profiles", {}) if isinstance(raw, dict) else {}
            if not isinstance(source, dict):
                return {}
            profiles: dict[str, dict[str, Any]] = {}
            for raw_uuid, item in source.items():
                key = self._canonical_uuid(raw_uuid)
                if not key or not isinstance(item, dict):
                    continue
                try:
                    rules = item.get("rule_counts", {})
                    if not isinstance(rules, dict):
                        rules = {}
                    profiles[key] = {
                        "trust": max(0.0, min(100.0, float(item.get("trust", 50.0)))),
                        "public_credit": clamp_public_credit(item.get("public_credit", PUBLIC_CREDIT_DEFAULT)),
                        "last_credit_penalty_at": max(0.0, float(item.get("last_credit_penalty_at", 0.0))),
                        "public_credit_recovery_seconds": max(0.0, min(3599.99, float(item.get("public_credit_recovery_seconds", 0.0)))),
                        "risk": max(0.0, min(100.0, float(item.get("risk", 0.0)))),
                        "flags": max(0, int(item.get("flags", 0))),
                        "strong_flags": max(0, int(item.get("strong_flags", 0))),
                        "rule_counts": {str(k)[:48]: max(0, int(v)) for k, v in list(rules.items())[:64]},
                        "last_flag_at": max(0.0, float(item.get("last_flag_at", 0.0))),
                        "last_seen_at": max(0.0, float(item.get("last_seen_at", 0.0))),
                        "active_seconds": max(0.0, float(item.get("active_seconds", 0.0))),
                        "last_rule": _clean(item.get("last_rule", ""), 48),
                    }
                except (TypeError, ValueError, OverflowError):
                    continue
            limit = max(100, int(self.cfg.get("profile_limit", 10000)))
            if len(profiles) > limit:
                profiles = dict(sorted(profiles.items(), key=lambda pair: pair[1].get("last_seen_at", 0.0), reverse=True)[:limit])
            return profiles
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.logger.warning(f"读取隐藏玩家画像失败，将重新建立：{type(exc).__name__}")
            return {}

    def _profile_for_uuid(self, player_uuid: str, create: bool = True) -> dict[str, Any] | None:
        key = self._canonical_uuid(player_uuid)
        if not key:
            return None
        profile = self.player_profiles.get(key)
        if profile is None and create:
            initial = max(0.0, min(100.0, float(self.cfg.get("trust_initial_score", 50.0))))
            profile = {
                "trust": initial,
                "public_credit": clamp_public_credit(self.cfg.get("public_credit_initial_score", PUBLIC_CREDIT_DEFAULT)),
                "last_credit_penalty_at": 0.0,
                "public_credit_recovery_seconds": 0.0,
                "risk": 0.0,
                "flags": 0,
                "strong_flags": 0,
                "rule_counts": {},
                "last_flag_at": 0.0,
                "last_seen_at": 0.0,
                "active_seconds": 0.0,
                "last_rule": "",
            }
            self.player_profiles[key] = profile
            self._profiles_dirty = True
        return profile

    def _save_profiles(self) -> None:
        """Persist only UUIDs and numeric/rule metadata; never a player's name or XUID."""
        profiles: dict[str, dict[str, Any]] = {}
        for key, item in self.player_profiles.items():
            normalized = self._canonical_uuid(key)
            if not normalized or not isinstance(item, dict):
                continue
            profiles[normalized] = {
                "trust": round(max(0.0, min(100.0, float(item.get("trust", 50.0)))), 2),
                "public_credit": round(clamp_public_credit(item.get("public_credit", PUBLIC_CREDIT_DEFAULT)), 2),
                "last_credit_penalty_at": float(item.get("last_credit_penalty_at", 0.0)),
                "public_credit_recovery_seconds": round(max(0.0, min(3599.99, float(item.get("public_credit_recovery_seconds", 0.0)))), 2),
                "risk": round(max(0.0, min(100.0, float(item.get("risk", 0.0)))), 2),
                "flags": max(0, int(item.get("flags", 0))),
                "strong_flags": max(0, int(item.get("strong_flags", 0))),
                "rule_counts": dict(list(item.get("rule_counts", {}).items())[:64]),
                "last_flag_at": float(item.get("last_flag_at", 0.0)),
                "last_seen_at": float(item.get("last_seen_at", 0.0)),
                "active_seconds": round(max(0.0, float(item.get("active_seconds", 0.0))), 2),
                "last_rule": _clean(item.get("last_rule", ""), 48),
            }
        limit = max(100, int(self.cfg.get("profile_limit", 10000)))
        if len(profiles) > limit:
            profiles = dict(sorted(profiles.items(), key=lambda pair: pair[1].get("last_seen_at", 0.0), reverse=True)[:limit])
        self._write_json_atomic(self.profiles_path, {"version": 1, "profiles": profiles})
        self._profiles_dirty = False
        self._profiles_last_save = time.monotonic()

    def _update_profile_flag(self, player_uuid: str, code: str, points: int, high_confidence: bool) -> dict[str, Any] | None:
        if not bool(self.cfg.get("trust_enabled", True)) and not bool(self.cfg.get("public_credit_enabled", True)):
            return None
        profile = self._profile_for_uuid(player_uuid)
        if profile is None:
            return None
        counts = profile.setdefault("rule_counts", {})
        count = int(counts.get(code, 0)) + 1
        counts[code] = count
        risk_delta, trust_delta = profile_penalty(points, count, high_confidence)
        if high_confidence:
            profile["strong_flags"] = int(profile.get("strong_flags", 0)) + 1
            if bool(self.cfg.get("trust_enabled", True)):
                profile["risk"] = min(100.0, float(profile.get("risk", 0.0)) + risk_delta)
                profile["trust"] = max(0.0, float(profile.get("trust", 50.0)) - trust_delta)
                profile["last_flag_at"] = time.time()
            if bool(self.cfg.get("public_credit_enabled", True)) and code in STRONG_RULES:
                penalty = max(0.0, float(self.cfg.get("public_credit_cheat_penalty", 12.0)))
                profile["public_credit"] = max(0.0, clamp_public_credit(profile.get("public_credit", PUBLIC_CREDIT_DEFAULT)) - penalty)
                profile["last_credit_penalty_at"] = time.time()
                profile["public_credit_recovery_seconds"] = 0.0
                self._log("public_credit_penalty", target_uuid=player_uuid, source="high_confidence_anticheat", rule=code, penalty=round(penalty, 2), public_credit=round(profile["public_credit"], 2))
        # 弱证据仍计入审计次数，但不惩罚持久风险/可信画像，也不重置画像恢复计时。
        profile["flags"] = int(profile.get("flags", 0)) + 1
        profile["last_rule"] = code
        # 只标记脏数据；由 _pulse 按 profile_save_interval_seconds 批量落盘。
        # 避免每条检测证据都触发同步磁盘 IO。
        self._profiles_dirty = True
        return profile

    def _update_profile_activity(self, player_uuid: str, now_epoch: float) -> None:
        if not bool(self.cfg.get("trust_enabled", True)) and not bool(self.cfg.get("public_credit_enabled", True)):
            return
        profile = self._profile_for_uuid(player_uuid)
        if profile is None:
            return
        last_seen = float(profile.get("last_seen_at", 0.0))
        delta = max(0.0, min(15.0, now_epoch - last_seen)) if last_seen > 0 else 0.0
        profile["last_seen_at"] = now_epoch
        last_flag = float(profile.get("last_flag_at", 0.0))
        if bool(self.cfg.get("trust_enabled", True)) and delta > 0 and now_epoch - last_flag >= max(0.0, float(self.cfg.get("trust_recovery_delay_seconds", 3600))):
            profile["active_seconds"] = float(profile.get("active_seconds", 0.0)) + delta
            trust_rate = max(0.0, float(self.cfg.get("trust_recovery_per_hour", 0.25)))
            profile["trust"] = min(100.0, float(profile.get("trust", 50.0)) + delta * trust_rate / 3600.0)
        if delta > 0 and bool(self.cfg.get("public_credit_enabled", True)):
            # Persist sub-hour online time so restarts never reset recovery progress.
            score, progress = accrue_public_credit(
                profile.get("public_credit", PUBLIC_CREDIT_DEFAULT),
                profile.get("public_credit_recovery_seconds", 0.0),
                delta,
                float(self.cfg.get("public_credit_recovery_per_hour", 0.5)),
            )
            profile["public_credit"] = score
            profile["public_credit_recovery_seconds"] = progress
        if bool(self.cfg.get("trust_enabled", True)) and delta > 0 and now_epoch - last_flag >= max(0.0, float(self.cfg.get("risk_decay_delay_seconds", 3600))):
            risk_rate = max(0.0, float(self.cfg.get("risk_decay_per_hour", 0.20)))
            profile["risk"] = max(0.0, float(profile.get("risk", 0.0)) - delta * risk_rate / 3600.0)
        self._profiles_dirty = True

    def _resolve_player_uuid(self, value: str) -> str | None:
        target = self._find_online_player(value)
        if target is not None:
            return _player_key(target)
        return self._canonical_uuid(value)

    def _save_candidates(self) -> None:
        # 本地管理员复核材料，严格不发送到云端 API。
        self._write_json_atomic(self.candidates_path, self.ban_candidates)

    @staticmethod
    def _canonical_uuid(value: Any) -> str | None:
        try:
            return str(uuidlib.UUID(str(value).strip()))
        except (ValueError, TypeError, AttributeError):
            return None

    def _cloud_config_ready(self) -> tuple[bool, str]:
        endpoint = str(self.cfg.get("cloud_endpoint", "") or "").strip().rstrip("/")
        read_token = str(self.cfg.get("cloud_read_token", "") or "").strip()
        if not endpoint:
            return False, "尚未配置 cloud_endpoint。"
        parsed = urllib.parse.urlparse(endpoint)
        local_http = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local_http):
            return False, "云端地址必须使用 HTTPS（仅 localhost 可使用 HTTP 测试）。"
        if not parsed.netloc:
            return False, "cloud_endpoint 格式无效。"
        if not read_token:
            return False, "尚未配置 cloud_read_token。"
        return True, ""

    def _cloud_request(
        self, path: str, token: str, method: str = "GET", payload: dict[str, Any] | None = None
    ) -> Any:
        base = str(self.cfg.get("cloud_endpoint", "") or "").strip().rstrip("/")
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"ZP-AC/{self.version}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=body, headers=headers, method=method)
        timeout = max(1.0, min(15.0, float(self.cfg.get("cloud_timeout_seconds", 4))))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise RuntimeError("云端响应超过 16 MiB 限制。")
                if not data:
                    return {}
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 不输出请求体或玩家身份数据，避免凭据/UUID进入日志。
            raise RuntimeError(f"云端 HTTP 状态码 {exc.code}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"云端连接失败：{type(exc.reason).__name__}") from None

    def _start_cloud_sync(self, source: str = "manual") -> bool:
        if self._closing or not bool(self.cfg.get("cloud_enabled", False)):
            return False
        ready, problem = self._cloud_config_ready()
        if not ready:
            self.logger.warning(f"云端同步未启动：{problem}")
            return False
        if not self._cloud_sync_lock.acquire(blocking=False):
            return False

        def worker() -> None:
            try:
                with self._state_lock:
                    local_bans = set(self.local_permanent_bans)
                    revocations = set(self.revoked_uuids)
                write_token = str(self.cfg.get("cloud_write_token", "") or "").strip()
                if write_token:
                    # 先应用撤销，再幂等上传本地已确认永久封禁 UUID。
                    if revocations:
                        for offset in range(0, len(revocations), 500):
                            batch = sorted(revocations)[offset:offset + 500]
                            self._cloud_request(
                                "/v1/bans", write_token, "DELETE", {"uuids": batch}
                            )
                        self._cloud_results.put(("revocations_done", revocations))
                    upload = sorted(local_bans - revocations)
                    for offset in range(0, len(upload), 500):
                        self._cloud_request(
                            "/v1/bans", write_token, "POST", {"uuids": upload[offset:offset + 500]}
                        )
                read_token = str(self.cfg.get("cloud_read_token", "") or "").strip()
                snapshot = self._cloud_request("/v1/bans", read_token, "GET")
                remote = snapshot.get("uuids", []) if isinstance(snapshot, dict) else []
                parsed = set()
                for item in remote:
                    normalized = self._canonical_uuid(item)
                    if normalized:
                        parsed.add(normalized)
                self._cloud_results.put(("snapshot", parsed))
                self._cloud_results.put(("sync_ok", source))
            except Exception as exc:
                self._cloud_results.put(("sync_error", str(exc)))
            finally:
                self._cloud_sync_lock.release()

        threading.Thread(target=worker, name="zp-ac-cloud-sync", daemon=True).start()
        return True

    def _start_cloud_check(self, player_uuid: str) -> None:
        if self._closing or not bool(self.cfg.get("cloud_enabled", False)):
            return
        ready, _ = self._cloud_config_ready()
        if not ready:
            return
        with self._cloud_check_lock:
            if player_uuid in self._cloud_checking:
                return
            self._cloud_checking.add(player_uuid)

        def worker() -> None:
            try:
                token = str(self.cfg.get("cloud_read_token", "") or "").strip()
                result = self._cloud_request("/v1/check", token, "POST", {"uuid": player_uuid})
                self._cloud_results.put(("join_check", (player_uuid, bool(result.get("banned", False)))))
            except Exception as exc:
                self._cloud_results.put(("check_error", str(exc)))
            finally:
                with self._cloud_check_lock:
                    self._cloud_checking.discard(player_uuid)

        threading.Thread(target=worker, name="zp-ac-cloud-check", daemon=True).start()

    def _remember_ban_candidate(self, session: dict[str, Any]) -> None:
        player_uuid = self._canonical_uuid(session.get("target_uuid"))
        if not player_uuid:
            return
        previous = self.ban_candidates.get(player_uuid, {})
        candidate = {
            "uuid": player_uuid,
            "player_name": _clean(session.get("target_name", ""), 64),
            "score": int(session.get("score", 0)),
            "reporter_count": len(session.get("reporters", {})),
            "rules": sorted(session.get("rule_codes", set())),
            "strong_rules": sorted(session.get("strong_codes", set())),
            "trust_score": round(float((self._profile_for_uuid(player_uuid) or {}).get("trust", 50.0)), 2),
            "risk_score": round(float((self._profile_for_uuid(player_uuid) or {}).get("risk", 0.0)), 2),
            "evidence": list(session.get("evidence", []))[-20:],
            "created_at": previous.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.ban_candidates[player_uuid] = candidate
        # 限制待复核文件增长；按更新时间保留最近 500 个候选。
        if len(self.ban_candidates) > 500:
            ordered = sorted(
                self.ban_candidates.items(),
                key=lambda item: str(item[1].get("updated_at", "")),
                reverse=True,
            )[:500]
            self.ban_candidates = dict(ordered)
        try:
            self._save_candidates()
        except Exception as exc:
            self.logger.warning(f"保存待复核封禁候选失败：{exc}")

    def _drain_cloud_results(self) -> None:
        while True:
            try:
                kind, payload = self._cloud_results.get_nowait()
            except queue.Empty:
                break
            if kind == "snapshot":
                with self._state_lock:
                    self.cloud_ban_cache = set(payload)
                    cache_copy = set(self.cloud_ban_cache)
                try:
                    self._save_uuid_set(self.cloud_cache_path, cache_copy)
                except Exception as exc:
                    self.logger.warning(f"保存云端 UUID 缓存失败：{exc}")
            elif kind == "revocations_done":
                with self._state_lock:
                    self.revoked_uuids.difference_update(payload)
                    self.cloud_ban_cache.difference_update(payload)
                    revoked_copy = set(self.revoked_uuids)
                    cache_copy = set(self.cloud_ban_cache)
                try:
                    self._save_uuid_set(self.revocations_path, revoked_copy)
                    self._save_uuid_set(self.cloud_cache_path, cache_copy)
                except Exception as exc:
                    self.logger.warning(f"保存云端撤销队列失败：{exc}")
            elif kind == "join_check":
                player_uuid, banned = payload
                if banned:
                    with self._state_lock:
                        self.cloud_ban_cache.add(player_uuid)
                        cache_copy = set(self.cloud_ban_cache)
                    try:
                        self._save_uuid_set(self.cloud_cache_path, cache_copy)
                    except Exception:
                        pass
                    player = next(
                        (p for p in list(self.server.online_players) if _player_key(p) == player_uuid),
                        None,
                    )
                    if player is not None and player_uuid not in self.revoked_uuids:
                        try:
                            player.kick("§c该 UUID 已存在于 ZP-AC 云端永久封禁名单中。")
                        except Exception as exc:
                            self.logger.warning(f"拒绝云端封禁 UUID 登录失败：{type(exc).__name__}")
            elif kind == "sync_ok":
                self._cloud_last_sync = time.monotonic()
                self.logger.info(f"云端 UUID 封禁名单同步完成（{payload}）。")
            elif kind in {"sync_error", "check_error"}:
                self.logger.warning(f"云端通信失败；本地检测继续运行：{payload}")

    def _set_cloud_enabled(self, enabled: bool) -> None:
        self.cfg["cloud_enabled"] = bool(enabled)
        self.config["cloud_enabled"] = bool(enabled)
        self.save_config()
        if enabled:
            self._cloud_last_sync = 0.0
            self._start_cloud_sync("admin_enable")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name == "zp":
            if isinstance(sender, ConsoleCommandSender):
                sender.send_message("§e/zp 只能由在线玩家执行。")
                return True
            player = self._find_online_player(_clean(getattr(sender, "name", ""), 64))
            player_uuid = _player_key(player) if player is not None else None
            if not player_uuid:
                sender.send_message("§c无法读取你的玩家 UUID，暂时无法查询信用分。")
                return True
            profile = self._profile_for_uuid(player_uuid)
            score = clamp_public_credit(profile.get("public_credit", PUBLIC_CREDIT_DEFAULT))
            # Player-facing command intentionally exposes only the public score.
            sender.send_message(f"§8[§bZP信用§8] §f公开信用分：§e{score:.1f}/100")
            return True
        if command.name != "zpac":
            return True
        if not self._is_admin_sender(sender):
            sender.send_message("§c没有权限执行 ZP-AC 管理命令。需要 OP 或 zp_ac_admin 管理员标签。")
            return True
        if not args:
            sender.send_message("§e用法：/zpac credit <玩家|UUID> | /zpac trust <玩家|UUID> | /zpac top [数量] | /zpac cloud <on|off|status|sync> | /zpac candidates | /zpac confirmban <UUID> | /zpac pardon <UUID>")
            return True
        action = str(args[0]).lower()
        value = str(args[1]).strip() if len(args) > 1 else ""
        if action == "credit":
            if not self._is_admin_sender(sender):
                sender.send_message("§c没有权限查询其他玩家的公开信用分。")
                return True
            player_uuid = self._resolve_player_uuid(value) if value else None
            if not player_uuid:
                sender.send_message("§e用法：/zpac credit <在线玩家名|UUID>。仅管理员可查询他人公开信用分。")
                return True
            profile = self._profile_for_uuid(player_uuid, create=False)
            score = clamp_public_credit(
                profile.get("public_credit", self.cfg.get("public_credit_initial_score", PUBLIC_CREDIT_DEFAULT))
                if profile else self.cfg.get("public_credit_initial_score", PUBLIC_CREDIT_DEFAULT)
            )
            sender.send_message(f"§8[§bZP信用管理§8] §f目标公开信用分：§e{score:.1f}/100")
            return True

        if action in {"trust", "profile"}:
            player_uuid = self._resolve_player_uuid(value) if value else None
            if not player_uuid:
                sender.send_message("§e用法：/zpac trust <在线玩家名|UUID>。可信分只对管理员显示。")
                return True
            profile = self._profile_for_uuid(player_uuid, create=False)
            if profile is None:
                initial = max(0.0, min(100.0, float(self.cfg.get("trust_initial_score", 50.0))))
                sender.send_message(f"§e该 UUID 尚无画像记录：可信分={initial:.1f}/100（中性初始值），风险分=0.0/100，累计证据=0。")
                return True
            sender.send_message(
                f"§8[§cZP-AC 内部画像§8] §7UUID={player_uuid}\n"
                f"§a隐藏可信分：§f{float(profile.get('trust', 50.0)):.1f}/100  "
                f"§c风险分：§f{float(profile.get('risk', 0.0)):.1f}/100\n"
                f"§7累计证据：{int(profile.get('flags', 0))}  强证据：{int(profile.get('strong_flags', 0))}  "
                f"活跃监控时间：{int(float(profile.get('active_seconds', 0.0)) // 60)} 分钟\n"
                f"§7最近规则：{_clean(profile.get('last_rule', '无'), 48) or '无'}。分数不等同作弊概率，且不会发送给玩家客户端。"
            )
            return True

        if action == "top":
            count = 10
            try:
                count = max(1, min(10, int(value or 10)))
            except ValueError:
                pass
            rows = sorted(self.player_profiles.items(), key=lambda pair: float(pair[1].get("risk", 0.0)), reverse=True)
            rows = [row for row in rows if float(row[1].get("risk", 0.0)) > 0][:count]
            if not rows:
                sender.send_message("§a当前没有累计风险分的玩家画像。")
            else:
                sender.send_message("§e风险画像排行（仅管理员可见；使用 /zpac trust <UUID> 查看）：")
                for player_uuid, profile in rows:
                    sender.send_message(
                        f"§7{player_uuid} §c风险 {float(profile.get('risk', 0.0)):.1f} "
                        f"§a可信 {float(profile.get('trust', 50.0)):.1f} §7证据 {int(profile.get('flags', 0))}"
                    )
            return True

        if action == "cloud":
            sub = value.lower()
            if sub == "on":
                ready, problem = self._cloud_config_ready()
                if not ready:
                    sender.send_message(f"§c无法启用云端：{problem}")
                else:
                    self._set_cloud_enabled(True)
                    sender.send_message("§a云端 UUID 封禁检查已启用；正在同步。")
            elif sub == "off":
                self._set_cloud_enabled(False)
                sender.send_message("§e云端功能已关闭；本地永久封禁仍然生效。")
            elif sub == "status":
                sender.send_message(
                    f"§e云端：{'启用' if self.cfg.get('cloud_enabled') else '关闭'}；"
                    f"读取令牌：{'已配置' if self.cfg.get('cloud_read_token') else '未配置'}；"
                    f"写入令牌：{'已配置' if self.cfg.get('cloud_write_token') else '未配置（只读模式）'}；"
                    f"本地永久封禁 UUID：{len(self.local_permanent_bans)}；"
                    f"云端缓存 UUID：{len(self.cloud_ban_cache)}；"
                    f"待撤销 UUID：{len(self.revoked_uuids)}。"
                )
            elif sub == "sync":
                if not bool(self.cfg.get("cloud_enabled", False)):
                    sender.send_message("§c云端功能当前关闭。先执行 /zpac cloud on。")
                elif self._start_cloud_sync("admin_manual"):
                    sender.send_message("§a已在后台启动云端同步，不会阻塞服务器主线程。")
                else:
                    sender.send_message("§e同步未启动：可能正在同步，或云端配置不完整。")
            else:
                sender.send_message("§e用法：/zpac cloud <on|off|status|sync>")
            return True

        if action == "candidates":
            with self._state_lock:
                items = sorted(
                    self.ban_candidates.items(),
                    key=lambda item: str(item[1].get("updated_at", "")),
                    reverse=True,
                )[:10]
            if not items:
                sender.send_message("§a当前没有达到严重行为门槛的待复核候选。")
            else:
                sender.send_message("§e待复核严重行为候选（仅列出最近 10 条）：")
                for player_uuid, record in items:
                    sender.send_message(
                        f"§f{_clean(record.get('player_name', ''), 32)} §7UUID={player_uuid} "
                        f"分数={record.get('score', 0)} 规则={','.join(record.get('rules', []))}"
                    )
            return True

        if action == "confirmban":
            player_uuid = self._canonical_uuid(value)
            if not player_uuid:
                sender.send_message("§c请提供有效 UUID。用法：/zpac confirmban <UUID>")
                return True
            with self._state_lock:
                candidate = self.ban_candidates.get(player_uuid)
                if not candidate:
                    sender.send_message("§c未找到该 UUID 的严重行为待复核记录；不能通过此命令直接加入云端名单。")
                    return True
                self.local_permanent_bans.add(player_uuid)
                self.revoked_uuids.discard(player_uuid)
                self.ban_candidates.pop(player_uuid, None)
                local_copy = set(self.local_permanent_bans)
                revoked_copy = set(self.revoked_uuids)
            try:
                self._save_uuid_set(self.local_bans_path, local_copy)
                self._save_uuid_set(self.revocations_path, revoked_copy)
                self._save_candidates()
            except Exception as exc:
                sender.send_message(f"§c写入本地永久封禁失败：{type(exc).__name__}")
                return True
            target = next(
                (p for p in list(self.server.online_players) if _player_key(p) == player_uuid),
                None,
            )
            if target is not None:
                try:
                    target.kick("§c管理员已确认严重违规并执行永久封禁。")
                except Exception:
                    pass
            self._log("permanent_ban_confirmed", target_uuid=player_uuid, score=candidate.get("score"), rules=candidate.get("rules", []), action="local_uuid_ban")
            if bool(self.cfg.get("cloud_enabled", False)):
                self._start_cloud_sync("confirmed_permanent_ban")
                if str(self.cfg.get("cloud_write_token", "") or "").strip():
                    sender.send_message("§a已确认永久封禁并写入本地 UUID 名单；云端 UUID 上传已排队。云端只接收 UUID。")
                else:
                    sender.send_message("§e已确认永久封禁并写入本地名单，但未配置 cloud_write_token；补充写入令牌后执行 /zpac cloud sync 才会上传。")
            else:
                sender.send_message("§a已确认永久封禁并写入本地 UUID 名单；云端关闭，启用后会补传。")
            return True

        if action == "pardon":
            player_uuid = self._canonical_uuid(value)
            if not player_uuid:
                sender.send_message("§c请提供有效 UUID。用法：/zpac pardon <UUID>")
                return True
            with self._state_lock:
                existed = player_uuid in self.local_permanent_bans or player_uuid in self.cloud_ban_cache
                self.local_permanent_bans.discard(player_uuid)
                self.cloud_ban_cache.discard(player_uuid)
                self.revoked_uuids.add(player_uuid)
                self.ban_candidates.pop(player_uuid, None)
                profile = self._profile_for_uuid(player_uuid, create=False)
                if profile is not None:
                    profile["trust"] = max(50.0, float(self.cfg.get("trust_initial_score", 50.0)))
                    profile["risk"] = 0.0
                    profile["last_flag_at"] = 0.0
                    profile["last_rule"] = "admin_review_cleared"
                    self._profiles_dirty = True
                local_copy = set(self.local_permanent_bans)
                cache_copy = set(self.cloud_ban_cache)
                revoked_copy = set(self.revoked_uuids)
            try:
                self._save_uuid_set(self.local_bans_path, local_copy)
                self._save_uuid_set(self.cloud_cache_path, cache_copy)
                self._save_uuid_set(self.revocations_path, revoked_copy)
                self._save_candidates()
                if self._profiles_dirty:
                    self._save_profiles()
            except Exception as exc:
                sender.send_message(f"§c保存撤销记录失败：{type(exc).__name__}")
                return True
            if bool(self.cfg.get("cloud_enabled", False)):
                self._start_cloud_sync("admin_pardon")
                sender.send_message("§a已撤销本地 UUID 封禁，并向云端提交撤销；其他服务器将在同步后更新。")
            else:
                sender.send_message("§a已撤销本地 UUID 封禁；云端关闭，撤销请求会在启用云端后提交。")
            self._log("permanent_ban_revoked", target_uuid=player_uuid, existed=existed)
            return True

        sender.send_message("§e未知操作。用法：/zpac trust <玩家|UUID>、/zpac top、/zpac candidates、/zpac confirmban <UUID>、/zpac pardon <UUID>、/zpac cloud <on|off|status|sync>。")
        return True

    # ----------------------------- report bridge -----------------------------

    def _send_admin_response(
        self,
        admin_name: str,
        request_id: str,
        ok: bool,
        data: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        payload = {
            "version": BP_ADMIN_PROTOCOL,
            "requestId": request_id,
            "adminName": _clean(admin_name, 64),
            "ok": bool(ok),
            "data": data or {},
            "error": _clean(error, 240),
        }
        encoded = base64.b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        try:
            sent = self.server.dispatch_command(
                self.server.command_sender,
                f"scriptevent {BP_ADMIN_RESPONSE_ID} b64:{encoded}",
            )
            if not sent:
                self.logger.warning("ZP-AC 管理响应 ScriptEvent 未执行。")
        except Exception as exc:
            self.logger.warning(f"发送 ZP-AC 管理响应失败：{type(exc).__name__}")

    def _on_behavior_pack_checks_sync(self, event: ScriptMessageEvent) -> None:
        if not bool(self.cfg.get("behavior_pack_bridge_enabled", True)):
            return
        sender = getattr(event, "sender", None)
        sender_name = _clean(getattr(sender, "name", ""), 32).lower()
        if not isinstance(sender, ConsoleCommandSender) and sender_name not in {"server", "console"}:
            self.logger.warning("忽略非服务端来源的反作弊检测配置同步。")
            return
        try:
            payload = json.loads(unquote(event.message or ""))
            if not isinstance(payload, dict) or payload.get("version") != BP_CHECKS_PROTOCOL:
                return
            previous = self._anti_cheat_checks()
            checks = self._store_anti_cheat_checks(payload.get("checks"))
            if checks != previous:
                enabled = sum(1 for value in checks.values() if value)
                self.logger.info(f"已从行为包同步反作弊检测开关：{enabled}/{len(checks)} 项启用。")
        except Exception as exc:
            self.logger.warning(f"行为包检测配置同步失败：{_clean(str(exc), 160)}")

    def _on_behavior_pack_admin_request(self, event: ScriptMessageEvent) -> None:
        if not bool(self.cfg.get("behavior_pack_bridge_enabled", True)):
            return
        admin_name = ""
        request_id = ""
        try:
            payload = json.loads(unquote(event.message or ""))
            if not isinstance(payload, dict) or payload.get("version") != BP_ADMIN_PROTOCOL:
                return
            admin_name = _clean(payload.get("adminName"), 64)
            request_id = _clean(payload.get("requestId"), 80)
            action = _clean(payload.get("action"), 32).lower()
            if not admin_name or not request_id:
                return

            requester = self._find_online_player(admin_name)
            if requester is None or not self._is_admin_player(requester):
                self._send_admin_response(admin_name, request_id, False, error="管理员身份验证失败。")
                return

            # Requests emitted by the behavior-pack server command are console-originated.
            # Direct player-originated ScriptEvents must match the authenticated OP/tagged player.
            sender = getattr(event, "sender", None)
            sender_key = _player_key(sender)
            if sender_key:
                if sender_key != _player_key(requester) or not self._is_admin_player(sender):
                    self._send_admin_response(admin_name, request_id, False, error="请求来源与管理员身份不匹配。")
                    return
            elif not isinstance(sender, ConsoleCommandSender) and _clean(getattr(sender, "name", ""), 32).lower() not in {"server", "console"}:
                self._send_admin_response(admin_name, request_id, False, error="不接受非服务端来源的管理请求。")
                return

            now = time.monotonic()
            previous = self._admin_request_last.get(_player_key(requester) or admin_name, 0.0)
            if now - previous < 0.75:
                self._send_admin_response(admin_name, request_id, False, error="管理操作过于频繁，请稍后再试。")
                return
            self._admin_request_last[_player_key(requester) or admin_name] = now

            value = _clean(payload.get("value"), 128)
            player_uuid = self._canonical_uuid(payload.get("uuid"))
            data: dict[str, Any]

            if action == "set_checks":
                data = {"checks": self._store_anti_cheat_checks(payload.get("checks"))}
            elif action == "overview":
                cloud_ready, cloud_problem = self._cloud_config_ready()
                data = {
                    "plugin_version": self.version,
                    "active_sessions": len(self.sessions),
                    "candidate_count": len(self.ban_candidates),
                    "local_ban_count": len(self.local_permanent_bans),
                    "profile_count": len(self.player_profiles),
                    "cloud_ban_count": len(self.cloud_ban_cache),
                    "cloud_enabled": bool(self.cfg.get("cloud_enabled", False)),
                    "cloud_config_ready": cloud_ready,
                    "cloud_problem": cloud_problem,
                }
            elif action == "candidate_list":
                candidates = sorted(
                    self.ban_candidates.items(),
                    key=lambda item: str(item[1].get("updated_at", "")),
                    reverse=True,
                )[:6]
                data = {
                    "candidates": [
                        {
                            "uuid": player_key,
                            "player_name": _clean(record.get("player_name", ""), 24),
                            "score": int(record.get("score", 0)),
                            "reporter_count": int(record.get("reporter_count", 0)),
                            "rules": [_clean(rule, 48) for rule in record.get("rules", [])[:4]],
                        }
                        for player_key, record in candidates
                    ]
                }
            elif action == "bans":
                data = {"uuids": sorted(self.local_permanent_bans)[:12]}
            elif action == "trust":
                resolved = self._resolve_player_uuid(value)
                if not resolved:
                    raise ValueError("未找到在线玩家或有效 UUID。")
                profile = self._profile_for_uuid(resolved, create=False)
                if profile is None:
                    data = {
                        "uuid": resolved,
                        "trust": float(self.cfg.get("trust_initial_score", 50.0)),
                        "risk": 0.0,
                        "flags": 0,
                        "strong_flags": 0,
                        "active_seconds": 0.0,
                        "last_rule": "",
                    }
                else:
                    data = {
                        "uuid": resolved,
                        "trust": float(profile.get("trust", 50.0)),
                        "risk": float(profile.get("risk", 0.0)),
                        "flags": int(profile.get("flags", 0)),
                        "strong_flags": int(profile.get("strong_flags", 0)),
                        "active_seconds": float(profile.get("active_seconds", 0.0)),
                        "last_rule": _clean(profile.get("last_rule", ""), 48),
                    }
            elif action == "confirmban":
                if not player_uuid:
                    raise ValueError("UUID 格式无效。")
                with self._state_lock:
                    candidate = self.ban_candidates.get(player_uuid)
                    if not candidate:
                        raise ValueError("没有该 UUID 的严重行为待复核候选，不能直接封禁。")
                    self.local_permanent_bans.add(player_uuid)
                    self.revoked_uuids.discard(player_uuid)
                    self.ban_candidates.pop(player_uuid, None)
                    local_copy = set(self.local_permanent_bans)
                    revoked_copy = set(self.revoked_uuids)
                self._save_uuid_set(self.local_bans_path, local_copy)
                self._save_uuid_set(self.revocations_path, revoked_copy)
                self._save_candidates()
                target = next(
                    (p for p in list(self.server.online_players) if _player_key(p) == player_uuid),
                    None,
                )
                if target is not None:
                    try:
                        target.kick("§c管理员已通过 ZP-AC 管理界面确认永久 UUID 封禁。")
                    except Exception:
                        pass
                self._log("permanent_ban_confirmed", target_uuid=player_uuid,
                          score=candidate.get("score"), rules=candidate.get("rules", []),
                          action="behavior_pack_admin_ui")
                if bool(self.cfg.get("cloud_enabled", False)):
                    self._start_cloud_sync("admin_ui_confirmed_permanent_ban")
                data = {"message": "永久 UUID 封禁已写入本地；云端同步按当前配置执行。"}
            elif action == "pardon":
                if not player_uuid:
                    raise ValueError("UUID 格式无效。")
                with self._state_lock:
                    existed = player_uuid in self.local_permanent_bans or player_uuid in self.cloud_ban_cache
                    self.local_permanent_bans.discard(player_uuid)
                    self.cloud_ban_cache.discard(player_uuid)
                    self.revoked_uuids.add(player_uuid)
                    self.ban_candidates.pop(player_uuid, None)
                    profile = self._profile_for_uuid(player_uuid, create=False)
                    if profile is not None:
                        profile["trust"] = max(50.0, float(self.cfg.get("trust_initial_score", 50.0)))
                        profile["risk"] = 0.0
                        profile["last_flag_at"] = 0.0
                        profile["last_rule"] = "admin_review_cleared"
                        self._profiles_dirty = True
                    local_copy = set(self.local_permanent_bans)
                    cache_copy = set(self.cloud_ban_cache)
                    revoked_copy = set(self.revoked_uuids)
                self._save_uuid_set(self.local_bans_path, local_copy)
                self._save_uuid_set(self.cloud_cache_path, cache_copy)
                self._save_uuid_set(self.revocations_path, revoked_copy)
                self._save_candidates()
                if self._profiles_dirty:
                    self._save_profiles()
                if bool(self.cfg.get("cloud_enabled", False)):
                    self._start_cloud_sync("admin_ui_pardon")
                self._log("permanent_ban_revoked", target_uuid=player_uuid, existed=existed,
                          action="behavior_pack_admin_ui")
                data = {"message": "已撤销本地 UUID 封禁并重置内部画像；云端撤销按同步设置执行。"}
            elif action == "cloud_status":
                ready, problem = self._cloud_config_ready()
                data = {
                    "enabled": bool(self.cfg.get("cloud_enabled", False)),
                    "read_token_configured": bool(str(self.cfg.get("cloud_read_token", "") or "").strip()),
                    "write_token_configured": bool(str(self.cfg.get("cloud_write_token", "") or "").strip()),
                    "local_ban_count": len(self.local_permanent_bans),
                    "cloud_ban_count": len(self.cloud_ban_cache),
                    "config_ready": ready,
                    "problem": problem,
                }
            elif action == "cloud_on":
                ready, problem = self._cloud_config_ready()
                if not ready:
                    raise ValueError(f"云端配置不完整：{problem}")
                self._set_cloud_enabled(True)
                data = {"message": "云端 UUID 同步已启用，后台同步已启动。"}
            elif action == "cloud_off":
                self._set_cloud_enabled(False)
                data = {"message": "云端同步已关闭；本地封禁仍然有效。"}
            elif action == "cloud_sync":
                if not bool(self.cfg.get("cloud_enabled", False)):
                    raise ValueError("云端功能当前关闭。")
                if not self._start_cloud_sync("admin_ui_manual"):
                    raise ValueError("同步未启动：可能正在同步或配置不完整。")
                data = {"message": "云端同步已在后台启动。"}
            else:
                raise ValueError("不支持的管理操作。")

            self._send_admin_response(admin_name, request_id, True, data=data)
        except Exception as exc:
            self._send_admin_response(
                admin_name, request_id, False,
                error=_clean(str(exc) or type(exc).__name__, 240),
            )

    def _announce_behavior_pack_ready(self) -> bool:
        if not bool(self.cfg.get("behavior_pack_bridge_enabled", True)):
            return False
        try:
            sent = self.server.dispatch_command(
                self.server.command_sender,
                f"scriptevent {BP_READY_ID} {BP_PROTOCOL}",
            )
            if not sent:
                self.logger.warning("ZP-AC 行为包握手命令未执行；将在下一轮重试。")
            return bool(sent)
        except Exception as exc:
            self.logger.warning(f"向 ZP-AC 行为包发送握手响应失败: {type(exc).__name__}: {exc}")
            return False

    def _on_behavior_pack_evidence(self, event: ScriptMessageEvent) -> None:
        if not bool(self.cfg.get("behavior_pack_bridge_enabled", True)):
            return
        try:
            payload = json.loads(unquote(event.message or ""))
        except Exception as exc:
            self.logger.warning(f"收到格式错误的行为包证据: {exc}")
            return
        if not isinstance(payload, dict) or payload.get("version") != BP_PROTOCOL:
            return

        target_name = _clean(payload.get("targetName"), 64)
        target = self._find_online_player(target_name)
        if target is None or self._is_exempt(target):
            return
        target_key = _player_key(target)
        if not target_key:
            return

        bp_code = _clean(payload.get("code"), 48)
        if bp_code == "large_displacement":
            self._log(
                "behavior_pack_telemetry",
                target=_player_name(target),
                target_uuid=target_key,
                code=bp_code,
                detail=_clean(payload.get("detail"), 360),
                dimension=_clean(payload.get("dimension"), 48),
                location=_clean(payload.get("location"), 64),
            )
            return

        mapped = BP_RULE_MAP.get(bp_code)
        if mapped is None:
            return
        code, may_be_strong = mapped
        title = _clean(payload.get("title"), 100)
        detail = _clean(payload.get("detail"), 360)
        session = self.sessions.get(target_key)
        if session is None:
            # 行为包全员监控无需先有玩家举报；reporter 集合保持为空，
            # 不能把目标本人伪装成举报者，也不能让举报数量抬高处罚等级。
            session = self._new_session(target, target, "behavior_pack_autonomous", "")
            session["reporters"].clear()
            session["reasons"] = ["behavior_pack_autonomous"]
            session["autonomous"] = True
            self.sessions[target_key] = session

        high_confidence = may_be_strong and bool(payload.get("highConfidence"))
        dimension = _clean(payload.get("dimension"), 48)
        location = _clean(payload.get("location"), 64)
        full_detail = f"[行为包/{bp_code}] {title}: {detail}"
        if dimension or location:
            full_detail += f"；位置 {dimension} {location}"
        self._flag(target, session, code, full_detail, high_confidence)

    @event_handler
    def on_script_message(self, event: ScriptMessageEvent) -> None:
        message_id = getattr(event, "message_id", "")
        if message_id == BP_CHECKS_SYNC_ID:
            self._on_behavior_pack_checks_sync(event)
            return
        if message_id == BP_ADMIN_REQUEST_ID:
            self._on_behavior_pack_admin_request(event)
            return
        if message_id == BP_EVIDENCE_ID:
            self._on_behavior_pack_evidence(event)
            return
        if message_id != MESSAGE_ID:
            return
        try:
            raw_message = event.message or ""
            if raw_message.startswith("b64:"):
                decoded = base64.b64decode(raw_message[4:], validate=True).decode("utf-8")
            else:
                # 兼容旧版 URL-encoded JSON bridge。
                decoded = unquote(raw_message)
            payload = json.loads(decoded)
        except Exception as exc:
            self.logger.warning(f"收到格式错误的举报消息: {exc}")
            return
        if not isinstance(payload, dict) or payload.get("version") != REPORT_VERSION:
            self.logger.warning("忽略版本不匹配或格式不正确的举报消息。")
            return

        reporter_name = _clean(payload.get("reporterName"), 64)
        target_name = _clean(payload.get("targetName"), 64)
        reason = _clean(payload.get("reason"), 200)
        report_id = _clean(payload.get("reportId"), 80)
        sender = event.sender
        sender_key = _player_key(sender)
        # 维度 runCommand 触发的 scriptevent 使用服务器命令上下文，没有玩家 UUID；
        # 若未来改为玩家上下文发送，则必须与 payload 中的举报者 UUID 对应。
        reporter = self._find_online_player(reporter_name)
        target = self._find_online_player(target_name)
        if reporter is None or target is None:
            self.logger.warning("忽略举报消息：举报者或目标玩家已离线。")
            return
        if sender_key and _player_key(reporter) != sender_key:
            self.logger.warning("忽略举报消息：举报者身份校验失败。")
            return
        if _player_key(reporter) == _player_key(target):
            return
        if self._is_exempt(target):
            try:
                reporter.send_message("§e举报已收到，但该目标属于管理员/豁免账号，不会启动反作弊监控。")
            except Exception:
                pass
            return

        now = time.monotonic()
        target_key = _player_key(target)
        reporter_key = _player_key(reporter) or reporter_name.casefold()
        cooldown_key = (target_key or target_name.casefold(), reporter_key)
        cooldown = float(self.cfg.get("reporter_target_cooldown_seconds", 600))
        session = self.sessions.get(target_key or "")
        if session is not None:
            # 同一举报者冷却期内重复提交，只更新理由/记录，不增加独立举报人数。
            last_report = self.report_cooldowns.get(cooldown_key, 0.0)
            is_new_reporter = reporter_key not in session["reporters"]
            if is_new_reporter:
                session["reporters"][reporter_key] = reporter_name
                session["last_evidence"] = now
            elif now - last_report >= cooldown:
                # 冷却后允许更新记录，但该账号仍只算一名独立举报者。
                session["last_evidence"] = now
            if reason and reason not in session["reasons"]:
                session["reasons"].append(reason)
                session["reasons"] = session["reasons"][-10:]
            if report_id:
                session["report_ids"].append(report_id)
                session["report_ids"] = session["report_ids"][-50:]
        else:
            session = self._new_session(target, reporter, reason, report_id)
            self.sessions[target_key or ""] = session
            is_new_reporter = True

        self.report_cooldowns[cooldown_key] = now
        tier = self._tier(len(session["reporters"]))
        self._admin_alert(
            f"已接收举报：§e{target_name}§r；独立举报者 {len(session['reporters'])} 人；检测等级 T{tier}。"
            + (f" 举报理由：{reason}" if reason else "")
        )
        self._log(
            "report_received",
            target=target_name,
            target_uuid=target_key,
            reporter=reporter_name,
            reporter_count=len(session["reporters"]),
            tier=tier,
            reason=reason,
            report_id=report_id,
            new_distinct_reporter=is_new_reporter,
        )

    # ----------------------------- movement checks -----------------------------

    @event_handler
    def on_player_move(self, event: PlayerMoveEvent) -> None:
        player = event.player
        key = _player_key(player)
        session = self.sessions.get(key or "")
        if session is None or session["kicked"] or self._is_exempt(player):
            return

        now = time.monotonic()
        if now < session["teleport_until"]:
            return
        origin = _xyz(event.from_location)
        destination = _xyz(event.to_location)
        if origin is None or destination is None:
            return
        dim = self._dimension_key(player)
        if dim != session["last_dimension"]:
            self._reset_motion(session, destination, dim, now)
            return

        if not self._check_enabled("speed"):
            session["move_window_start"] = now
            session["move_window_distance"] = 0.0
            session["speed_windows"] = 0
            session["last_position"] = destination
            return

        delta = _horizontal_distance(origin, destination)
        vertical = abs(destination[1] - origin[1])
        # 大位移不视为 Speed：可能是传送、回弹、服务器修正或其他插件。
        if delta > 8.0 or vertical > 8.0:
            self._reset_motion(session, destination, dim, now)
            session["teleport_until"] = now + float(self.cfg.get("teleport_grace_seconds", 3))
            self._log("movement_discontinuity_ignored", target=session["target_name"], horizontal=round(delta, 2), vertical=round(vertical, 2))
            return

        session["move_window_distance"] += delta
        elapsed = now - session["move_window_start"]
        if elapsed < 1.0:
            return

        speed = session["move_window_distance"] / max(0.05, elapsed)
        session["move_window_start"] = now
        session["move_window_distance"] = 0.0
        try:
            grounded = bool(player.is_on_ground)
            swimming = bool(player.is_in_water) or bool(getattr(player, "is_swimming", False))
            in_lava = bool(player.is_in_lava)
            flying = bool(player.is_flying)
            gliding = bool(getattr(player, "is_gliding", False))
            allow_flight = bool(getattr(player, "allow_flight", False))
        except Exception:
            # 状态 API 不可用时不猜测玩家处于正常移动状态：清空窗口并跳过本次处罚判断。
            session["speed_windows"] = 0
            return

        speed_amplifier = read_effect_amplifier(player, "speed")
        levitation_amplifier = read_effect_amplifier(player, "levitation")
        slow_falling_amplifier = read_effect_amplifier(player, "slow_falling")
        if None in (speed_amplifier, levitation_amplifier, slow_falling_amplifier):
            session["speed_windows"] = 0
            return
        potion_factor = speed_effect_multiplier(speed_amplifier)
        special_movement = (
            swimming or in_lava or flying or gliding or allow_flight
            or levitation_amplifier >= 0 or slow_falling_amplifier >= 0
        )
        comparable_ground_movement = grounded and not special_movement
        if bool(self.cfg.get("ai_enabled", True)) and comparable_ground_movement:
            # 先按速度药水等级归一化，再送入个人基线，避免药水状态污染 AI。
            self.ai_engine.observe(key or "", "move_speed", speed / potion_factor, now)

        tier = self._tier(len(session["reporters"]))
        base_threshold = self._tier_value("speed_thresholds", tier, [16.5, 15.5, 14.5, 13.5])
        try:
            ping = max(0, min(1000, int(getattr(player, "ping", 0))))
        except Exception:
            ping = 0
        try:
            walk_speed = float(getattr(player, "walk_speed", 0.1))
        except Exception:
            walk_speed = 0.1
        custom_speed_factor = max(1.0, min(1.5, walk_speed / 0.1)) if math.isfinite(walk_speed) and walk_speed > 0 else 1.0
        ping_start = max(0, int(self.cfg.get("ping_speed_threshold_start", 150)))
        ping_scale = max(0.0, float(self.cfg.get("ping_speed_threshold_scale", 0.00045)))
        ping_factor = 1.0 + min(0.25, max(0, ping - ping_start) * ping_scale)
        threshold = base_threshold * custom_speed_factor * ping_factor * potion_factor
        if comparable_ground_movement and speed >= threshold:
            session["speed_windows"] += 1
            required = int(self._tier_value("speed_windows_required", tier, [3, 3, 3, 2]))
            if ping >= 250:
                required += max(0, int(self.cfg.get("ping_speed_window_bonus", 1)))
            if session["speed_windows"] >= required:
                extreme = speed >= threshold * float(self.cfg.get("speed_extreme_multiplier", 1.65)) and ping < 250
                self._flag(
                    player,
                    session,
                    "speed_sustained",
                    f"连续 {session['speed_windows']} 个移动窗口超出阈值；当前 {speed:.1f} 格/秒，基础阈值 {base_threshold:.1f}，"
                    f"延迟修正阈值 {threshold:.1f}；玩家 ping={ping}ms，行走速度系数={custom_speed_factor:.2f}，速度药水倍率={potion_factor:.2f}。",
                    high_confidence=extreme,
                )
                session["speed_windows"] = 0
        else:
            session["speed_windows"] = max(0, session["speed_windows"] - 1)

    def _reset_motion(self, session: dict[str, Any], pos: tuple[float, float, float], dim: str, now: float) -> None:
        session["last_position"] = pos
        session["last_dimension"] = dim
        session["move_window_start"] = now
        session["move_window_distance"] = 0.0
        session["speed_windows"] = 0
        session["air_since"] = None
        session["air_start_y"] = None
        session["air_start_xz"] = None

    @event_handler
    def on_player_teleport(self, event: PlayerTeleportEvent) -> None:
        player = event.player
        session = self.sessions.get(_player_key(player) or "")
        if session is None:
            return
        now = time.monotonic()
        pos = _xyz(event.to_location) or _xyz(getattr(player, "location", None)) or (0.0, 0.0, 0.0)
        self._reset_motion(session, pos, self._dimension_key(player), now)
        session["teleport_until"] = now + float(self.cfg.get("teleport_grace_seconds", 3))

    @event_handler
    def on_player_respawn(self, event: PlayerRespawnEvent) -> None:
        """Respawn relocates a player; discard the old movement/scaffold sample window."""
        player = event.player
        session = self.sessions.get(_player_key(player) or "")
        if session is None:
            return
        now = time.monotonic()
        pos = _xyz(getattr(player, "location", None)) or session.get("last_position") or (0.0, 0.0, 0.0)
        grace = max(3.0, float(self.cfg.get("spawn_grace_seconds", 5.0)))
        self._reset_motion(session, pos, self._dimension_key(player), now)
        session["teleport_until"] = now + grace
        for name in ("scaffold_events", "reach_soft", "reach_hard", "combat_hits"):
            values = session.get(name)
            if values is not None:
                values.clear()
        self._log(
            "movement_baseline_reset",
            target_uuid=session.get("target_uuid"),
            reason="player_respawn",
            grace_seconds=grace,
        )

    @event_handler
    def on_dimension_change(self, event: PlayerDimensionChangeEvent) -> None:
        player = event.player
        session = self.sessions.get(_player_key(player) or "")
        if session is not None:
            now = time.monotonic()
            pos = _xyz(getattr(player, "location", None)) or (0.0, 0.0, 0.0)
            self._reset_motion(session, pos, self._dimension_key(player), now)
            session["teleport_until"] = now + float(self.cfg.get("teleport_grace_seconds", 3))

    @event_handler
    def on_gamemode_change(self, event: PlayerGameModeChangeEvent) -> None:
        player = event.player
        session = self.sessions.get(_player_key(player) or "")
        if session is not None:
            now = time.monotonic()
            pos = _xyz(getattr(player, "location", None)) or (0.0, 0.0, 0.0)
            self._reset_motion(session, pos, self._dimension_key(player), now)
            session["teleport_until"] = now + 2.0

    # ----------------------------- combat checks -----------------------------

    @event_handler
    def on_actor_damage(self, event: ActorDamageEvent) -> None:
        reach_enabled = self._check_enabled("reach")
        combat_enabled = self._check_enabled("combat")
        try:
            if bool(getattr(event, "is_cancelled", False)):
                return
            source = event.damage_source
            attacker = source.damaging_actor
            victim = event.actor
            attacker_key = _player_key(attacker)
            victim_key = _player_key(victim)
            if not attacker_key or not victim_key or attacker_key == victim_key:
                return
            # Resolve both actors against the online player list so mobs and other
            # non-player actors can never be blocked by the PvP credit gate.
            online_attacker = self._find_online_player(_player_name(attacker))
            online_victim = self._find_online_player(_player_name(victim))
            if online_attacker is None or online_victim is None:
                return
            if _player_key(online_attacker) != attacker_key or _player_key(online_victim) != victim_key:
                return
            attacker, victim = online_attacker, online_victim
            now_pvp = time.monotonic()
            # A player may retaliate against the same attacker for five seconds.
            retaliation_key = (attacker_key, victim_key)
            retaliated_at = self.pvp_last_hit_at.get(retaliation_key, 0.0)
            retaliation_window = max(0.0, float(self.cfg.get("public_credit_retaliation_seconds", 5.0)))
            is_retaliation = is_retaliation_allowed(retaliated_at, now_pvp, retaliation_window)
            if bool(self.cfg.get("public_credit_enabled", True)):
                attacker_profile = self._profile_for_uuid(attacker_key)
                credit = clamp_public_credit(attacker_profile.get("public_credit", PUBLIC_CREDIT_DEFAULT))
                threshold = float(self.cfg.get("public_credit_lock_threshold", PUBLIC_CREDIT_LOCK_THRESHOLD))
                if not can_damage_players(credit, threshold, False) and not is_retaliation:
                    event.is_cancelled = True
                    now_message = time.monotonic()
                    if now_message - self.pvp_message_cooldowns.get(attacker_key, 0.0) >= 2.0:
                        try:
                            attacker.send_action_bar("§c信用分过低")
                        except Exception:
                            attacker.send_message("§c信用分过低")
                        self.pvp_message_cooldowns[attacker_key] = now_message
                    return
                if should_count_pvp_hit(is_retaliation):
                    self._record_public_pvp_hit(attacker_key, victim_key)
                else:
                    # Mark the exchange as reciprocal so the original attacker is
                    # not penalized for a one-sided assault after the victim fights back.
                    reciprocal = self.pvp_recent_hits[(attacker_key, victim_key)]
                    reciprocal.append(now_pvp)
                    while reciprocal and now_pvp - reciprocal[0] > max(
                        5.0, float(self.cfg.get("public_credit_assault_window_seconds", 20.0))
                    ):
                        reciprocal.popleft()
            # Record allowed incoming damage as a five-second retaliation opportunity.
            self.pvp_last_hit_at[(victim_key, attacker_key)] = now_pvp
            for old_key, old_time in list(self.pvp_last_hit_at.items()):
                if now_pvp - old_time > max(5.0, retaliation_window * 2.0):
                    self.pvp_last_hit_at.pop(old_key, None)
            if not reach_enabled and not combat_enabled:
                return
            if bool(source.is_indirect):
                return
            session = self.sessions.get(attacker_key)
            if session is None or session["kicked"]:
                return
            a = _xyz(attacker.location)
            b = _xyz(victim.location)
            if a is None or b is None:
                return
            now = time.monotonic()
            distance = _distance(a, b)
            tier = self._tier(len(session["reporters"]))
            soft = self._tier_value("reach_soft_thresholds", tier, [5.8, 5.6, 5.3, 5.0])
            hard = self._tier_value("reach_hard_thresholds", tier, [7.2, 6.9, 6.6, 6.3])
            try:
                attacker_ping = max(0, min(1000, int(getattr(attacker, "ping", 0))))
            except Exception:
                attacker_ping = 0
            try:
                victim_ping = max(0, min(1000, int(getattr(victim, "ping", 0))))
            except Exception:
                victim_ping = 0
            allowance_scale = max(0.0, float(self.cfg.get("reach_ping_allowance_scale", 0.0010)))
            allowance_cap = max(0.0, float(self.cfg.get("reach_ping_allowance_max", 0.90)))
            latency_allowance = min(allowance_cap, (attacker_ping + victim_ping) * allowance_scale)
            soft += latency_allowance
            hard += latency_allowance

            if combat_enabled:
                session["combat_hits"].append((now, victim_key, _player_name(victim), distance))
                if bool(self.cfg.get("ai_enabled", True)):
                    self.ai_engine.observe(attacker_key, "combat_hit", 1.0, now)
                    self.ai_engine.observe(attacker_key, "combat_target", float(hash(victim_key) % 1000000), now)
                self._prune_deque(session["combat_hits"], now - 5.0)

            if reach_enabled and distance >= hard:
                session["reach_hard"].append((now, distance, victim_key))
                self._prune_deque(session["reach_hard"], now - 6.0)
                if len(session["reach_hard"]) >= int(self.cfg.get("reach_hard_hits_required", 2)):
                    max_distance = max(item[1] for item in session["reach_hard"])
                    self._flag(
                        attacker,
                        session,
                        "reach_extreme",
                        f"近战命中距离多次达到硬阈值；{len(session['reach_hard'])} 次命中，最大位置距离 {max_distance:.2f} 格，"
                        f"延迟修正硬阈值 {hard:.2f} 格；攻击者/目标 ping={attacker_ping}/{victim_ping}ms，容差 {latency_allowance:.2f} 格。",
                        high_confidence=max_distance >= hard * 1.12,
                    )
                    session["reach_hard"].clear()
            elif reach_enabled and distance >= soft:
                session["reach_soft"].append((now, distance, victim_key))
                self._prune_deque(session["reach_soft"], now - 5.0)
                if len(session["reach_soft"]) >= int(self.cfg.get("reach_soft_hits_required", 4)):
                    max_distance = max(item[1] for item in session["reach_soft"])
                    self._flag(
                        attacker,
                        session,
                        "reach_repeated",
                        f"5 秒内 {len(session['reach_soft'])} 次近战命中达到延迟修正软阈值 {soft:.2f} 格；最大位置距离 {max_distance:.2f} 格；"
                        f"攻击者/目标 ping={attacker_ping}/{victim_ping}ms。",
                        high_confidence=False,
                    )
                    session["reach_soft"].clear()

            if combat_enabled:
                window = float(self.cfg.get("combat_window_seconds", 2.0))
                recent = [hit for hit in session["combat_hits"] if now - hit[0] <= window]
                distinct_targets = {hit[1] for hit in recent}
                hits_required = int(self._tier_value("combat_hits_required", tier, [8, 8, 7, 6]))
                targets_required = int(self._tier_value("combat_targets_required", tier, [3, 3, 3, 2]))
                if len(recent) >= hits_required and len(distinct_targets) >= targets_required:
                    names = sorted({hit[2] for hit in recent if hit[2]})
                    self._flag(
                        attacker,
                        session,
                        "multi_target_combat",
                        f"约 {window:.1f} 秒内记录到 {len(recent)} 次直接近战伤害，涉及 {len(distinct_targets)} 名玩家（{', '.join(names[:5])}）。",
                        high_confidence=True,
                    )
                    session["combat_hits"].clear()
            else:
                session["combat_hits"].clear()
        except Exception as exc:
            self.logger.warning(f"处理攻击事件失败: {exc}")

    def _record_public_pvp_hit(self, attacker_uuid: str, victim_uuid: str) -> None:
        """Penalize repeated one-sided PvP harassment, not reciprocal combat."""
        now = time.monotonic()
        window = max(5.0, float(self.cfg.get("public_credit_assault_window_seconds", 20.0)))
        pair = (attacker_uuid, victim_uuid)
        reverse = (victim_uuid, attacker_uuid)
        hits = self.pvp_recent_hits[pair]
        hits.append(now)
        while hits and now - hits[0] > window:
            hits.popleft()
        reverse_hits = self.pvp_recent_hits.get(reverse, deque())
        while reverse_hits and now - reverse_hits[0] > window:
            reverse_hits.popleft()
        required = max(3, int(self.cfg.get("public_credit_assault_hits_required", 8)))
        cooldown = max(window, float(self.cfg.get("public_credit_assault_cooldown_seconds", 600.0)))
        if len(hits) < required or reverse_hits or now - self.pvp_penalty_cooldowns.get(pair, 0.0) < cooldown:
            return
        profile = self._profile_for_uuid(attacker_uuid)
        if profile is None:
            return
        penalty = max(0.0, float(self.cfg.get("public_credit_assault_penalty", 5.0)))
        after = max(0.0, clamp_public_credit(profile.get("public_credit", PUBLIC_CREDIT_DEFAULT)) - penalty)
        profile["public_credit"] = after
        profile["last_credit_penalty_at"] = time.time()
        profile["public_credit_recovery_seconds"] = 0.0
        self._profiles_dirty = True
        self.pvp_penalty_cooldowns[pair] = now
        hits.clear()
        self._log("public_credit_penalty", target_uuid=attacker_uuid, victim_uuid=victim_uuid,
                  source="repeated_one_sided_pvp", hit_count=required, penalty=round(penalty, 2),
                  public_credit=round(after, 2))
        attacker = next((p for p in list(self.server.online_players) if _player_key(p) == attacker_uuid), None)
        if attacker is not None:
            try:
                attacker.send_message(f"§e[ZP信用系统] 检测到短时间内持续单方面攻击玩家，信用分降低 {penalty:.1f} 分。当前信用分：{after:.1f}/100。")
            except Exception:
                pass

    @staticmethod
    def _prune_deque(items: deque, cutoff: float) -> None:
        while items and items[0][0] < cutoff:
            items.popleft()

    @event_handler
    def on_block_break(self, event: BlockBreakEvent) -> None:
        player = event.player
        key = _player_key(player)
        session = self.sessions.get(key or "")
        if (
            session is None
            or session["kicked"]
            or self._is_exempt(player)
            or bool(getattr(event, "is_cancelled", False))
        ):
            return

        now = time.monotonic()

        # Server-authoritative ore/rock mining sequence analysis. Weak evidence only.
        if self._check_enabled("xray"):
            try:
                block = event.block
                block_type = str(block.type).lower().split(":")[-1]
                ore_types = {
                    "coal_ore", "deepslate_coal_ore", "copper_ore", "deepslate_copper_ore",
                    "iron_ore", "deepslate_iron_ore", "gold_ore", "deepslate_gold_ore",
                    "redstone_ore", "deepslate_redstone_ore", "lapis_ore", "deepslate_lapis_ore",
                    "diamond_ore", "deepslate_diamond_ore", "emerald_ore",
                    "deepslate_emerald_ore", "ancient_debris",
                }
                rock_tokens = (
                    "stone", "deepslate", "netherrack", "blackstone", "basalt",
                    "tuff", "granite", "diorite", "andesite", "calcite", "gravel",
                )
                is_ore = block_type in ore_types
                is_rock = any(token in block_type for token in rock_tokens)
                if is_ore or is_rock:
                    mining = session.setdefault("xray_mining_events", deque())
                    dimension = self._dimension_key(player)
                    mining.append((
                        now, dimension, int(block.x), int(block.y), int(block.z),
                        block_type, "ore" if is_ore else "rock",
                    ))
                    if bool(self.cfg.get("ai_enabled", True)):
                        self.ai_engine.observe(key or "", "ore" if is_ore else "rock", 1.0, now)
                    window = max(30.0, float(self.cfg.get("xray_window_seconds", 180.0)))
                    while mining and now - mining[0][0] > window:
                        mining.popleft()

                    recent = [row for row in mining if row[1] == dimension]
                    ore_rows = [row for row in recent if row[6] == "ore"]
                    rock_count = sum(1 for row in recent if row[6] == "rock")
                    ore_count = len(ore_rows)
                    rare_names = {
                        "diamond_ore", "deepslate_diamond_ore",
                        "emerald_ore", "deepslate_emerald_ore", "ancient_debris",
                    }
                    rare_count = sum(1 for row in ore_rows if row[5] in rare_names)
                    anchors: list[tuple[float, float, float]] = []
                    cluster_distance = max(3.0, float(self.cfg.get("xray_cluster_distance", 5.5)))
                    for row in ore_rows:
                        point = (float(row[2]), float(row[3]), float(row[4]))
                        if all(_distance(point, anchor) > cluster_distance for anchor in anchors):
                            anchors.append(point)

                    total_mined = ore_count + rock_count
                    ore_ratio = ore_count / total_mined if total_mined else 0.0
                    enough_evidence = (
                        rock_count >= int(self.cfg.get("xray_min_rock_blocks", 40))
                        and ore_count >= int(self.cfg.get("xray_min_ore_blocks", 5))
                        and rare_count >= int(self.cfg.get("xray_min_rare_ores", 2))
                        and len(anchors) >= int(self.cfg.get("xray_min_clusters", 3))
                        and ore_ratio >= float(self.cfg.get("xray_min_ore_ratio", 0.08))
                    )
                    if enough_evidence:
                        self._flag(
                            player,
                            session,
                            "xray_mining_pattern",
                            (
                                f"最近 {window:.0f} 秒挖掘 {rock_count} 个岩石类方块、"
                                f"{ore_count} 个矿物方块（高价值矿物 {rare_count} 个），"
                                f"分布约 {len(anchors)} 个独立矿脉，矿物占比 {ore_ratio:.1%}。"
                                "这是异常定向采矿辅助线索；洞穴、矿脉密集区、效率工具和地图生成均可能造成相似结果。"
                            ),
                            high_confidence=False,
                        )
                        mining.clear()
            except Exception as exc:
                self.logger.debug(f"矿物透视行为分析跳过一次方块事件: {exc}")

        if not self._check_enabled("fast_break"):
            return
        events = self.break_events[key]
        events.append(now)
        window = float(self.cfg.get("rapid_break_window_seconds", 2.0))
        while events and now - events[0] > window:
            events.popleft()
        tier = self._tier(len(session["reporters"]))
        break_threshold = int(self._tier_value("rapid_break_count", tier, [22, 20, 18, 16]))
        if len(events) >= break_threshold:
            self._flag(
                player,
                session,
                "rapid_block_break",
                f"约 {window:.1f} 秒内完成 {len(events)} 次方块破坏；该项仅为辅助证据，可能受创造模式、效率附魔、急迫效果或地图机制影响。",
                high_confidence=False,
            )
            events.clear()

    # ----------------------------- behavior pattern checks -----------------------------

    @event_handler
    def on_player_interact(self, event: PlayerInteractEvent) -> None:
        """Observe successful API-level left-click actions; weak signal only."""
        player = event.player
        key = _player_key(player)
        session = self.sessions.get(key or "")
        if session is None or session["kicked"] or self._is_exempt(player) or not self._check_enabled("autoclick"):
            return
        try:
            action = str(getattr(event.action, "name", event.action)).upper()
        except Exception:
            return
        if "LEFT_CLICK" not in action:
            return

        now = time.monotonic()
        clicks = self.click_events[key]
        if clicks and bool(self.cfg.get("ai_enabled", True)):
            self.ai_engine.observe(key or "", "click_interval", now - clicks[-1], now)
        clicks.append(now)
        window = float(self.cfg.get("autoclick_window_seconds", 2.5))
        while clicks and now - clicks[0] > window:
            clicks.popleft()
        required = int(self.cfg.get("autoclick_clicks_required", 52))
        if len(clicks) < required:
            return

        intervals = [clicks[i] - clicks[i - 1] for i in range(1, len(clicks))]
        if len(intervals) < required - 1:
            return
        mean = sum(intervals) / len(intervals)
        if mean <= 0:
            return
        variance = sum((value - mean) ** 2 for value in intervals) / len(intervals)
        coefficient = math.sqrt(variance) / mean
        max_cv = float(self.cfg.get("autoclick_max_interval_cv", 0.12))
        if coefficient <= max_cv:
            self._flag(
                player,
                session,
                "autoclick_pattern",
                f"{len(clicks)} 次左键操作集中在 {window:.1f} 秒内；平均间隔 {mean * 1000:.1f} ms；间隔变异系数 {coefficient:.3f}。该项可能受输入设备/网络影响，仅作辅助线索。",
                high_confidence=False,
            )
            clicks.clear()

    @event_handler
    def on_block_place(self, event: BlockPlaceEvent) -> None:
        """Detect a repeated moving bridge/scaffold-like placement pattern."""
        player = event.player
        key = _player_key(player)
        session = self.sessions.get(key or "")
        if session is None or session["kicked"] or self._is_exempt(player) or not self._check_enabled("scaffold"):
            return
        try:
            state = event.block_placed_state
            block_loc = _xyz(state.location)
            player_loc = _xyz(player.location)
            if block_loc is None or player_loc is None:
                return
            # Only placements close to the player's feet are relevant to scaffold.
            if not (player_loc[1] - 2.0 <= block_loc[1] <= player_loc[1] + 0.35):
                return
            if _horizontal_distance(block_loc, player_loc) > 2.25:
                return
        except Exception:
            return

        now = time.monotonic()
        events = session["scaffold_events"]
        events.append((now, block_loc, player_loc))
        if bool(self.cfg.get("ai_enabled", True)):
            self.ai_engine.observe(key or "", "scaffold_place", 1.0, now)
        window = float(self.cfg.get("scaffold_window_seconds", 3.5))
        while events and now - events[0][0] > window:
            events.popleft()
        required = int(self.cfg.get("scaffold_placements_required", 7))
        if len(events) < required:
            return
        unique_blocks = {(round(row[1][0]), round(row[1][1]), round(row[1][2])) for row in events}
        player_positions = [row[2] for row in events]
        travel = _horizontal_distance(player_positions[0], player_positions[-1])
        y_values = [pos[1] for pos in player_positions]
        vertical_spread = max(y_values) - min(y_values)
        if len(unique_blocks) >= required - 1 and travel >= float(self.cfg.get("scaffold_min_travel", 3.5)) and vertical_spread <= float(self.cfg.get("scaffold_max_vertical_spread", 1.25)):
            self._flag(
                player,
                session,
                "scaffold_pattern",
                f"{len(events)} 次近脚部放置，涉及 {len(unique_blocks)} 个不同方块位置；同期水平移动 {travel:.2f} 格，垂直波动 {vertical_spread:.2f} 格。建筑/跑酷也可能产生类似行为，仅作辅助线索。",
                high_confidence=False,
            )
            events.clear()

    # ----------------------------- packet observation -----------------------------

    @event_handler
    def on_packet_receive(self, event: PacketReceiveEvent) -> None:
        if not bool(self.cfg.get("packet_telemetry", True)):
            return
        try:
            player = event.player
            key = _player_key(player)
            session = self.sessions.get(key or "")
            if session is None:
                return
            second = int(time.monotonic())
            if second != session["packet_second_start"]:
                session["packet_second_start"] = second
                session["packet_total"] = 0
                session["packet_counts"].clear()
            packet_id = int(event.packet_id)
            session["packet_total"] += 1
            session["packet_counts"][packet_id] += 1
            session["packet_totals"][packet_id] += 1
            payload = getattr(event, "payload", b"") or b""
            payload_size = len(payload)
            session["packet_payload_bytes"][packet_id] += payload_size
            session["packet_payload_max"][packet_id] = max(
                session["packet_payload_max"][packet_id], payload_size
            )
        except Exception:
            # 包观察器是辅助遥测，不能影响服务器主逻辑。
            return

    # ----------------------------- evidence, scoring, kick -----------------------------

    def _flag(self, player: Any, session: dict[str, Any], code: str, detail: str, high_confidence: bool) -> None:
        check_key = RULE_CHECK_GROUP.get(code)
        if check_key and not self._check_enabled(check_key):
            return
        now = time.monotonic()
        last = session["rule_last_at"].get(code, 0.0)
        if now - last < 12.0:
            return
        session["rule_last_at"][code] = now
        session["last_evidence"] = now
        session["rule_codes"].add(code)
        session["rule_counts"][code] += 1
        count = session["rule_counts"][code]
        points = BASE_POINTS.get(code, 1) + min(2, max(0, count - 1))
        session["score"] += points
        if high_confidence and code in STRONG_RULES:
            session["strong_codes"].add(code)

        title = RULE_TITLES.get(code, code)
        profile = self._update_profile_flag(session.get("target_uuid") or "", code, points, high_confidence)
        trust_score = float(profile.get("trust", 50.0)) if profile else None
        risk_score = float(profile.get("risk", 0.0)) if profile else None
        evidence = {
            "time": datetime.now(timezone.utc).isoformat(),
            "code": code,
            "title": title,
            "detail": _clean(detail, 360),
            "points": points,
            "high_confidence": bool(high_confidence),
        }
        session["evidence"].append(evidence)
        session["evidence"] = session["evidence"][-30:]
        self._admin_alert(
            f"§c检测异常§r：§e{session['target_name']}§r | {title} | {detail} | "
            f"会话嫌疑分 {session['score']}，独立举报者 {len(session['reporters'])} 人。"
            + (f" 内部可信分 {trust_score:.1f} / 风险分 {risk_score:.1f}。" if profile else "")
        )
        self._log(
            "evidence_flag",
            target=session["target_name"],
            target_uuid=session["target_uuid"],
            code=code,
            title=title,
            detail=_clean(detail, 360),
            points=points,
            score=session["score"],
            reporter_count=len(session["reporters"]),
            high_confidence=bool(high_confidence),
            trust_score=round(trust_score, 2) if trust_score is not None else None,
            risk_score=round(risk_score, 2) if risk_score is not None else None,
        )
        self._maybe_kick(player, session)

    def _maybe_kick(self, player: Any, session: dict[str, Any]) -> None:
        if session["kicked"]:
            return
        reporter_count = len(session["reporters"])
        tier = self._tier(reporter_count)
        threshold = self._tier_value("kick_score_thresholds", tier, [15, 14, 13, 12])
        if session["score"] < threshold:
            return
        if len(session["rule_codes"]) < 2:
            return
        if not session["strong_codes"]:
            return
        minimum_strong = max(2, int(self.cfg.get("auto_kick_min_strong_rules", 2)))
        if not has_independent_strong_evidence(
            session["rule_codes"], session["strong_codes"], minimum_strong
        ):
            self._admin_alert(
                f"§e暂缓自动踢出§r：{session['target_name']}；当前强证据仅 "
                f"{len(session['strong_codes'])}/{minimum_strong} 项，弱辅助线索不参与自动处罚。"
            )
            self._log(
                "auto_kick_deferred_insufficient_independent_strong_rules",
                target_uuid=session.get("target_uuid"),
                session_score=session.get("score", 0),
                rule_codes=sorted(session.get("rule_codes", set())),
                strong_codes=sorted(session.get("strong_codes", set())),
                required_strong_rules=minimum_strong,
                action="review_only_no_kick",
            )
            return

        # 严重检测只产生本地待复核候选；永久封禁仍必须由管理员显式确认。
        self._remember_ban_candidate(session)
        if not bool(self.cfg.get("auto_kick", True)):
            return
        profile = self._profile_for_uuid(session.get("target_uuid") or "", create=False) or {}
        risk_score = float(profile.get("risk", 0.0))
        trust_enabled = bool(self.cfg.get("trust_enabled", True))
        risk_threshold = max(0.0, float(self.cfg.get("auto_kick_risk_threshold", 22.0))) if trust_enabled else 0.0
        if risk_score < risk_threshold:
            self._admin_alert(
                f"§e已加入待复核候选§r：{session['target_name']}；会话嫌疑分达到门槛，但持续风险分 "
                f"{risk_score:.1f}/{risk_threshold:.1f}，暂不自动踢出。"
            )
            self._log(
                "auto_kick_deferred_risk_gate",
                target_uuid=session.get("target_uuid"),
                session_score=session.get("score", 0),
                risk_score=round(risk_score, 2),
                risk_threshold=risk_threshold,
                rules=sorted(session.get("rule_codes", set())),
            )
            return

        # 公告原因只列出已有证据支持的独立规则，不显示举报者身份。
        codes = sorted(session["rule_codes"], key=lambda c: BASE_POINTS.get(c, 0), reverse=True)
        titles = [RULE_TITLES.get(code, code) for code in codes[:3]]
        reason_text = "、".join(titles)
        target_name = session["target_name"]
        announcement = (
            f"§8[§c反作弊公告§8] §e{target_name}§r 因检测到多项异常行为已被服务器踢出。"
            f"§7检测行为：{reason_text}。本次为踢出处理，未执行封禁。"
        )
        kick_message = (
            "§c你已被服务器反作弊系统踢出。\n"
            "§f触发行为：\n§e" + "\n§e".join(titles) +
            f"\n§7嫌疑分：{session['score']}；独立举报者：{reporter_count}。"
            "\n§7本次为踢出，不是封禁。如认为检测有误，请联系服务器管理员复核。"
        )

        session["kicked"] = True
        try:
            # 先执行踢出；成功后再公告，避免踢出失败时向全服发布错误信息。
            player.kick(kick_message)
        except Exception as exc:
            session["kicked"] = False
            self.logger.error(f"踢出 {target_name} 失败: {exc}")
            self._admin_alert(f"§c自动踢出执行失败§r：{target_name}；请管理员手动处理。")
            return

        # 逐个通知踢出后的所有在线玩家，避免普通玩家看不到公告。
        for online in list(self.server.online_players):
            try:
                online.send_message(announcement)
            except Exception:
                pass
        self._log(
            "player_kicked",
            target=target_name,
            target_uuid=session["target_uuid"],
            score=session["score"],
            reporter_count=reporter_count,
            behavior_titles=titles,
            evidence=session["evidence"][-12:],
            action="kick_only_no_ban",
        )
        self.sessions.pop(session["target_uuid"], None)

    # ----------------------------- session lifecycle -----------------------------

    def _pulse(self) -> None:
        self._drain_cloud_results()
        now = time.monotonic()
        self._sync_operator_admin_tags()
        if bool(self.cfg.get("behavior_pack_bridge_enabled", True)) and now - self._bp_last_ready >= 30.0:
            self._bp_last_ready = now
            self._announce_behavior_pack_ready()
        if bool(self.cfg.get("cloud_enabled", False)):
            interval = max(15.0, float(self.cfg.get("cloud_sync_interval_seconds", 60)))
            if now - self._cloud_last_sync >= interval:
                self._start_cloud_sync("periodic")
        online = list(self.server.online_players)
        profile_now = time.time()
        for online_player in online:
            player_uuid = _player_key(online_player)
            if player_uuid:
                self._update_profile_activity(player_uuid, profile_now)
            self._ensure_autonomous_session(online_player)
        save_interval = max(15.0, float(self.cfg.get("profile_save_interval_seconds", 60)))
        if self._profiles_dirty and now - self._profiles_last_save >= save_interval:
            try:
                self._save_profiles()
            except Exception as exc:
                self.logger.warning(f"定期保存隐藏玩家画像失败：{type(exc).__name__}")
        online_by_id = {_player_key(player): player for player in online if _player_key(player)}
        for key, session in list(self.sessions.items()):
            player = online_by_id.get(key)
            if player is None:
                if session["offline_since"] is None:
                    session["offline_since"] = now
                grace = float(self.cfg.get("offline_grace_seconds", 8))
                if now - session["offline_since"] >= grace:
                    self._end_session(key, session, "offline")
                continue

            session["offline_since"] = None
            session["last_seen"] = now
            self._sample_air_state(player, session, now)
            self._run_ai_analysis(player, session, now)
            if session["kicked"]:
                continue

            elapsed = now - session["started"]
            quiet = now - session["last_evidence"]
            max_seconds = float(self.cfg.get("maximum_monitor_seconds", 600))
            min_seconds = float(self.cfg.get("minimum_monitor_seconds", 60))
            quiet_seconds = float(self.cfg.get("quiet_stop_seconds", 90))
            if session.get("autonomous"):
                # 全员常驻会话不因安静窗口自动结束；每隔 max_seconds 轮换起始时间，
                # 避免运行时间无限增长，同时保留当前行为统计窗口。
                if elapsed >= max_seconds:
                    session["started"] = now
                    session["last_evidence"] = now
                    self._log("autonomous_monitor_heartbeat", target=session["target_name"], target_uuid=key)
                continue
            if elapsed >= max_seconds:
                self._end_session(key, session, "maximum_time")
            elif elapsed >= min_seconds and quiet >= quiet_seconds:
                self._end_session(key, session, "clean")

    def _run_ai_analysis(self, player: Any, session: dict[str, Any], now: float) -> None:
        """Run local review-only behavior analysis; never changes risk or punishment state."""
        if not bool(self.cfg.get("ai_enabled", True)) or self._is_exempt(player):
            return
        interval = max(2.0, min(60.0, float(self.cfg.get("ai_analysis_interval_seconds", 5.0))))
        player_uuid = session.get("target_uuid") or ""
        if now - self._ai_last_analysis.get(player_uuid, 0.0) < interval:
            return
        self._ai_last_analysis[player_uuid] = now
        feature_vector = self.ai_engine.snapshot(player_uuid, now)
        if feature_vector:
            self.ai_dataset.record(player_uuid, feature_vector)
            onnx_result = self.ai_model.predict(feature_vector)
            if onnx_result and onnx_result["score"] >= float(self.cfg.get("ai_onnx_review_threshold", 0.90)):
                self._admin_alert(f"§dONNX 行为模型审核线索§r：§e{_player_name(player)}§r；模型异常分数 {onnx_result['score']:.3f}。仅供管理员复核。")
                self._log("ai_onnx_review", target_uuid=player_uuid, model=onnx_result["model"], score=onnx_result["score"], action="review_only_no_risk_no_punishment")
        result = self.ai_engine.analyze(player_uuid, now)
        if not result:
            return
        self._admin_alert(
            f"§d本地 AI 行为分析线索§r：§e{_player_name(player)}§r；"
            f"{result['feature_count']} 项行为特征同时偏离个人基线。仅供管理员复核，不构成作弊结论。"
        )
        self._log(
            "ai_behavior_anomaly",
            target_uuid=player_uuid,
            model="local_robust_baseline_v1",
            window_seconds=result["window_seconds"],
            baseline_samples=result["baseline_samples"],
            feature_count=result["feature_count"],
            features=result["features"],
            action="review_only_no_risk_no_punishment",
        )

    def _sample_air_state(self, player: Any, session: dict[str, Any], now: float) -> None:
        if not self._check_enabled("flight"):
            session["air_since"] = None
            session["air_start_y"] = None
            session["air_start_xz"] = None
            return
        if self._is_exempt(player) or now < session["teleport_until"]:
            session["air_since"] = None
            return
        try:
            in_water = bool(player.is_in_water) or bool(getattr(player, "is_swimming", False))
            in_lava = bool(player.is_in_lava)
            on_ground = bool(player.is_on_ground)
            flying = bool(player.is_flying)
            gliding = bool(getattr(player, "is_gliding", False))
            allow_flight = bool(getattr(player, "allow_flight", False))
            velocity = player.velocity
            vy = float(velocity.y)
            vx = float(velocity.x)
            vz = float(velocity.z)
        except Exception:
            session["air_since"] = None
            session["air_start_y"] = None
            session["air_start_xz"] = None
            return
        levitation_amplifier = read_effect_amplifier(player, "levitation")
        slow_falling_amplifier = read_effect_amplifier(player, "slow_falling")
        jump_amplifier = read_effect_amplifier(player, "jump_boost")
        if None in (levitation_amplifier, slow_falling_amplifier, jump_amplifier):
            session["air_since"] = None
            session["air_start_y"] = None
            session["air_start_xz"] = None
            return
        # 鞘翅、漂浮、缓降、跳跃提升、水中/岩浆及高速滑翔均不进入悬浮判定。
        if (
            in_water or in_lava or on_ground or flying or gliding or allow_flight
            or levitation_amplifier >= 0 or slow_falling_amplifier >= 0
            or jump_amplifier >= 0 or math.hypot(vx, vz) > 7.0
        ):
            session["air_since"] = None
            session["air_start_y"] = None
            return

        pos = _xyz(player.location)
        if pos is None:
            return
        if session["air_since"] is None:
            session["air_since"] = now
            session["air_start_y"] = pos[1]
            session["air_start_xz"] = (pos[0], pos[2])
            return

        duration = now - session["air_since"]
        vertical_delta = abs(pos[1] - float(session["air_start_y"] or pos[1]))
        tier = self._tier(len(session["reporters"]))
        hover_limit = self._tier_value("hover_seconds", tier, [4.0, 3.6, 3.2, 2.8])
        tolerance = float(self.cfg.get("hover_vertical_tolerance", 0.55))
        start_xz = session.get("air_start_xz") or (pos[0], pos[2])
        horizontal_drift = math.hypot(pos[0] - start_xz[0], pos[2] - start_xz[1])
        if duration >= hover_limit and vertical_delta <= tolerance and abs(vy) <= 0.06:
            session["hover_flags"] += 1
            high_conf = session["hover_flags"] >= 2 and horizontal_drift >= 1.0
            self._flag(
                player,
                session,
                "hover_confirmed",
                f"连续离地 {duration:.1f} 秒；垂直变化 {vertical_delta:.2f} 格；水平漂移 {horizontal_drift:.2f} 格；垂直速度 {vy:.3f}。",
                high_confidence=high_conf,
            )
            session["air_since"] = now
            session["air_start_y"] = pos[1]
            session["air_start_xz"] = (pos[0], pos[2])
        elif vertical_delta > tolerance * 1.5:
            session["air_since"] = now
            session["air_start_y"] = pos[1]
            session["air_start_xz"] = (pos[0], pos[2])

    def _end_session(self, key: str, session: dict[str, Any], status: str) -> None:
        self.sessions.pop(key, None)
        if status == "clean":
            message = f"§a监控结束§r：{session['target_name']} 在本次监控期内没有出现达到处理标准的新异常。"
        elif status == "offline":
            message = f"监控结束：{session['target_name']} 离线超过宽限时间。"
        else:
            message = f"监控结束：{session['target_name']} 已达到最长监控时间；已有记录保留供管理员复核。"
        self._admin_alert(message)
        self._log(
            "monitor_end",
            target=session["target_name"],
            target_uuid=key,
            status=status,
            reporter_count=len(session["reporters"]),
            score=session["score"],
            rules=sorted(session["rule_codes"]),
            evidence=session["evidence"][-20:],
            packet_totals=dict(session["packet_totals"].most_common(12)),
            packet_payload_bytes=dict(session["packet_payload_bytes"].most_common(12)),
            packet_payload_max=dict(session["packet_payload_max"].most_common(12)),
        )

    # ----------------------------- player lifecycle -----------------------------

    @event_handler
    def on_player_login(self, event: PlayerLoginEvent) -> None:
        key = _player_key(event.player)
        if not key:
            return
        with self._state_lock:
            local_banned = key in self.local_permanent_bans
            cloud_banned = (
                bool(self.cfg.get("cloud_enabled", False))
                and key in self.cloud_ban_cache
                and key not in self.revoked_uuids
            )
        if local_banned or cloud_banned:
            event.kick_message = "§c该 UUID 已被 ZP-AC 永久封禁。若有异议，请联系服务器管理员复核。"
            event.is_cancelled = True

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        try:
            if bool(getattr(event.player, "is_op", False)):
                event.player.add_scoreboard_tag(BP_OP_ADMIN_TAG)
            else:
                tags = set(getattr(event.player, "scoreboard_tags", []) or [])
                if BP_OP_ADMIN_TAG in tags:
                    event.player.remove_scoreboard_tag(BP_OP_ADMIN_TAG)
        except Exception:
            pass
        key = _player_key(event.player)
        if key:
            # Start a fresh online-time baseline on every join. Time spent offline
            # (including server downtime) must never count toward credit recovery.
            profile = self._profile_for_uuid(key)
            if profile is not None:
                profile["last_seen_at"] = time.time()
                self._profiles_dirty = True
        if key and key in self.sessions:
            self.sessions[key]["offline_since"] = None
            self.sessions[key]["teleport_until"] = time.monotonic() + max(3.0, float(self.cfg.get("spawn_grace_seconds", 5.0)))
        self._ensure_autonomous_session(event.player)
        if key and bool(self.cfg.get("cloud_enabled", False)):
            with self._state_lock:
                already_known = key in self.cloud_ban_cache or key in self.local_permanent_bans
                revoked = key in self.revoked_uuids
            if not already_known and not revoked:
                # 加入后异步复查最新云端状态；不在游戏主线程执行 HTTP。
                self._start_cloud_check(key)

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent) -> None:
        key = _player_key(event.player)
        if key and key in self.sessions:
            self.sessions[key]["offline_since"] = time.monotonic()

