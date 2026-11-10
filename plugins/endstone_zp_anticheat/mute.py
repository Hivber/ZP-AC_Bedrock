from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MUTE_DURATIONS_SECONDS = (600, 1800, 3600, 10800, 21600, 43200, 86400)


def duration_for_violation(count: int, durations: Iterable[int] = DEFAULT_MUTE_DURATIONS_SECONDS) -> int:
    """Return the configured duration for a 1-based violation count, capped at the last tier."""
    values = tuple(max(1, int(value)) for value in durations)
    if not values:
        values = DEFAULT_MUTE_DURATIONS_SECONDS
    index = max(0, int(count) - 1)
    return values[min(index, len(values) - 1)]


class MuteStore:
    """UUID-keyed, atomically persisted mute state. Expiry uses wall-clock timestamps."""

    def __init__(self, path: str | Path, durations: Iterable[int] = DEFAULT_MUTE_DURATIONS_SECONDS):
        self.path = Path(path)
        self.durations = tuple(max(1, int(value)) for value in durations) or DEFAULT_MUTE_DURATIONS_SECONDS
        self.records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            source = payload.get("players", {}) if isinstance(payload, dict) else {}
            if not isinstance(source, dict):
                return
            for player_uuid, raw in source.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    self.records[str(player_uuid)] = {
                        "violation_count": max(0, int(raw.get("violation_count", 0))),
                        "muted_until": max(0.0, float(raw.get("muted_until", 0.0))),
                        "updated_at": max(0.0, float(raw.get("updated_at", 0.0))),
                    }
                except (TypeError, ValueError, OverflowError):
                    continue
        except FileNotFoundError:
            return
        except Exception:
            # A damaged mute file must not prevent the server from starting.
            self.records = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "players": self.records}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def get(self, player_uuid: str) -> dict[str, Any]:
        return dict(self.records.get(str(player_uuid), {
            "violation_count": 0, "muted_until": 0.0, "updated_at": 0.0,
        }))

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
