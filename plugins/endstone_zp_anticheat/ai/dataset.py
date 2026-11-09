"""Bounded, privacy-minimized JSONL behavior-window dataset collector."""
from __future__ import annotations
import hashlib, json, math, os, time
from pathlib import Path
from typing import Any
try:
    from .onnx_engine import FEATURE_ORDER
except ImportError:
    from onnx_engine import FEATURE_ORDER

class BehaviorDatasetCollector:
    def __init__(self, path: str | Path, enabled: bool = True, interval_seconds: float = 30.0, max_bytes: int = 20_000_000) -> None:
        self.path=Path(path); self.enabled=bool(enabled)
        self.interval_seconds=max(10.0,min(3600.0,float(interval_seconds)))
        self.max_bytes=max(1_000_000,int(max_bytes)); self.last_write: dict[str,float]={}
    def record(self, player_uuid: str, features: dict[str,float], now: float | None=None) -> bool:
        if not self.enabled or not player_uuid: return False
        timestamp=time.time() if now is None else float(now)
        key=hashlib.sha256(player_uuid.encode("utf-8")).hexdigest()
        if timestamp-self.last_write.get(key,0.0)<self.interval_seconds: return False
        vector={}
        for name in FEATURE_ORDER:
            value=float(features.get(name,0.0))
            if not math.isfinite(value): return False
            vector[name]=round(value,6)
        row: dict[str,Any]={"schema":"zp-ac-behavior-v1","timestamp":int(timestamp),"player_key":key,"features":vector,"label":None}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.exists() and self.path.stat().st_size>=self.max_bytes:
            backup=self.path.with_suffix(self.path.suffix+".1")
            try:
                if backup.exists(): backup.unlink()
                os.replace(self.path,backup)
            except OSError: return False
        try:
            with self.path.open("a",encoding="utf-8") as stream:
                stream.write(json.dumps(row,separators=(",",":"),ensure_ascii=False)+"\n")
            self.last_write[key]=timestamp
            return True
        except OSError: return False
