"""Apply manually reviewed labels to ZP-AC JSONL samples.

Usage: python tools/label_ai_dataset.py data/ai_dataset.jsonl <player_key> normal|cheat
This intentionally labels all samples for one pseudonymous player key; review
the key and evidence offline before running. It never labels automatically.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

def main() -> int:
    if len(sys.argv) != 4 or sys.argv[3] not in {"normal", "cheat"}:
        print("Usage: python tools/label_ai_dataset.py <dataset.jsonl> <player_key> normal|cheat")
        return 2
    path, key, label = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key.lower()):
        print("player_key must be a 64-character SHA-256 hex value")
        return 2
    rows = []
    changed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("player_key") == key:
            row["label"] = label
            changed += 1
        rows.append(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    temp.replace(path)
    print(f"Updated {changed} samples. Labels are manual review annotations, not model verdicts.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
