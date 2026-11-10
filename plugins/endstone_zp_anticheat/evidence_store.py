"""Bounded, local-only evidence index and administrator review annotations."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EvidenceStore:
    """Append-only evidence records with separate, non-punitive review annotations."""

    def __init__(self, path: Path, reviews_path: Path, max_records: int = 10000) -> None:
        self.path = Path(path)
        self.reviews_path = Path(reviews_path)
        self.max_records = max(100, int(max_records))
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reviews_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except FileNotFoundError:
            pass
        return rows

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        row = dict(record)
        row["evidence_id"] = uuid.uuid4().hex[:16]
        row.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
        # Keep records bounded and JSON-safe; evidence never leaves the local server.
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            rows = self._read_jsonl(self.path)
            rows.append(json.loads(encoded))
            rows = rows[-self.max_records:]
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                for item in rows:
                    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            temporary.replace(self.path)
        return row

    def recent(self, player_uuid: str, limit: int = 10) -> list[dict[str, Any]]:
        count = max(1, min(50, int(limit)))
        with self._lock:
            rows = self._read_jsonl(self.path)
            matches = [row for row in rows if str(row.get("target_uuid", "")) == str(player_uuid)]
            reviews = self._load_reviews()
            result = []
            for row in matches[-count:]:
                item = dict(row)
                item["review"] = reviews.get(str(item.get("evidence_id", "")), {})
                result.append(item)
            return list(reversed(result))

    def _load_reviews(self) -> dict[str, Any]:
        try:
            value = json.loads(self.reviews_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def review(self, evidence_id: str, status: str, reviewer: str, note: str = "") -> dict[str, Any]:
        if status not in {"confirmed", "dismissed"}:
            raise ValueError("review status must be confirmed or dismissed")
        key = str(evidence_id).strip().lower()
        if len(key) != 16 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError("invalid evidence id")
        with self._lock:
            rows = self._read_jsonl(self.path)
            if not any(str(row.get("evidence_id", "")) == key for row in rows):
                raise KeyError("evidence record not found")
            reviews = self._load_reviews()
            item = {
                "status": status,
                "reviewer": str(reviewer)[:64],
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "note": str(note)[:240],
            }
            reviews[key] = item
            temporary = self.reviews_path.with_suffix(self.reviews_path.suffix + ".tmp")
            temporary.write_text(json.dumps(reviews, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.reviews_path)
            return item

    def count(self) -> int:
        with self._lock:
            return len(self._read_jsonl(self.path))
