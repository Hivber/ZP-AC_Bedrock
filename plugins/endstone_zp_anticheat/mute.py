from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MUTE_DURATIONS_SECONDS = (600, 1800, 3600, 10800, 21600, 43200, 86400)
COOLDOWN_RETENTION_SECONDS = 30 * 24 * 60 * 60


def duration_for_violation(count: int, durations: Iterable[int] = DEFAULT_MUTE_DURATIONS_SECONDS) -> int:
    """Return the configured duration for a 1-based violation count, capped at the last tier."""
    values = tuple(max(1, int(value)) for value in durations)
    if not values:
        values = DEFAULT_MUTE_DURATIONS_SECONDS
    index = max(0, int(count) - 1)
    return values[min(index, len(values) - 1)]


class MuteStore:
    """UUID-keyed mute state and persistent reporter/target cooldowns."""

    def __init__(self, path: str | Path, durations: Iterable[int] = DEFAULT_MUTE_DURATIONS_SECONDS):
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.durations = tuple(max(1, int(value)) for value in durations) or DEFAULT_MUTE_DURATIONS_SECONDS
        self.records: dict[str, dict[str, Any]] = {}
        # Keys are JSON-encoded [target_uuid, reporter_uuid] pairs; values are Unix timestamps.
        self.report_cooldowns: dict[str, float] = {}
        self._load()

    def _read_payload(self, path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("mute store root must be an object")
        raw_players = payload.get("players", {})
        raw_cooldowns = payload.get("report_cooldowns", {})
        if not isinstance(raw_players, dict) or not isinstance(raw_cooldowns, dict):
            raise ValueError("mute store sections must be objects")
        records: dict[str, dict[str, Any]] = {}
        for player_uuid, raw in raw_players.items():
            if not isinstance(raw, dict):
                continue
            try:
                records[str(player_uuid)] = {
                    "violation_count": max(0, int(raw.get("violation_count", 0))),
                    "muted_until": max(0.0, float(raw.get("muted_until", 0.0))),
                    "updated_at": max(0.0, float(raw.get("updated_at", 0.0))),
                }
            except (TypeError, ValueError, OverflowError):
                continue
        cooldowns: dict[str, float] = {}
        for key, value in raw_cooldowns.items():
            try:
                timestamp = float(value)
                if timestamp > 0:
                    cooldowns[str(key)] = timestamp
            except (TypeError, ValueError, OverflowError):
                continue
        return records, cooldowns

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.records, self.report_cooldowns = self._read_payload(self.path)
                return
            except Exception:
                # Keep the damaged original for diagnosis; never overwrite it with empty data.
                damaged = self.path.with_suffix(self.path.suffix + ".corrupt")
                try:
                    if damaged.exists():
                        damaged = self.path.with_suffix(self.path.suffix + f".corrupt.{int(time.time())}")
                    self.path.replace(damaged)
                except OSError:
                    pass
        if self.backup_path.exists():
            try:
                self.records, self.report_cooldowns = self._read_payload(self.backup_path)
                # Restore the primary file from the last known-good backup.
                self._save()
                return
            except Exception:
                # Both copies are retained; startup remains available for administrators.
                pass
        self.records = {}
        self.report_cooldowns = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "players": self.records,
            "report_cooldowns": self.report_cooldowns,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        backup_temporary = self.backup_path.with_suffix(self.backup_path.suffix + ".tmp")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        temporary.write_text(serialized, encoding="utf-8")
        if self.path.exists():
            shutil.copy2(self.path, backup_temporary)
            backup_temporary.replace(self.backup_path)
        elif self.backup_path.exists() is False:
            # First write: create a valid backup as well as the primary file.
            backup_temporary.write_text(serialized, encoding="utf-8")
            backup_temporary.replace(self.backup_path)
        temporary.replace(self.path)

    def get(self, player_uuid: str) -> dict[str, Any]:
        return dict(self.records.get(str(player_uuid), {
            "violation_count": 0, "muted_until": 0.0, "updated_at": 0.0,
        }))

    @staticmethod
    def _cooldown_key(target_uuid: str, reporter_uuid: str) -> str:
        return json.dumps([str(target_uuid), str(reporter_uuid)], ensure_ascii=False, separators=(",", ":"))

    def try_record_report(
        self,
        target_uuid: str,
        reporter_uuid: str,
        cooldown_seconds: float = 600.0,
        now: float | None = None,
    ) -> bool:
        """Persist an accepted insult report; rejected repeats never extend the cooldown."""
        current = time.time() if now is None else float(now)
        cooldown = max(0.0, float(cooldown_seconds))
        key = self._cooldown_key(target_uuid, reporter_uuid)
        previous = self.report_cooldowns.get(key)
        if previous is not None and current - previous < cooldown:
            return False
        self.report_cooldowns[key] = current
        cutoff = current - max(COOLDOWN_RETENTION_SECONDS, cooldown * 2)
        self.report_cooldowns = {
            item_key: timestamp
            for item_key, timestamp in self.report_cooldowns.items()
            if timestamp >= cutoff
        }
        self._save()
        return True

    def record_violation(self, player_uuid: str, now: float | None = None) -> dict[str, Any]:
        key = str(player_uuid)
        current = time.time() if now is None else float(now)
        previous = self.get(key)
        count = int(previous["violation_count"]) + 1
        duration = duration_for_violation(count, self.durations)
        # A new report refreshes the mute to at least the current tier from now.
        muted_until = max(float(previous["muted_until"]), current + duration)
        record = {
            "violation_count": count,
            "muted_until": muted_until,
            "updated_at": current,
        }
        self.records[key] = record
        self._save()
        return {**record, "duration_seconds": duration}

    def is_muted(self, player_uuid: str, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return float(self.records.get(str(player_uuid), {}).get("muted_until", 0.0)) > current

    def unmute(self, player_uuid: str) -> bool:
        key = str(player_uuid)
        record = self.records.get(key)
        if record is None or float(record.get("muted_until", 0.0)) <= 0:
            return False
        record["muted_until"] = 0.0
        record["updated_at"] = time.time()
        self._save()
        return True
