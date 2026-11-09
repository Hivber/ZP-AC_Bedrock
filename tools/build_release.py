from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ADDON_DIR = ROOT / "mcaddon"
ADDON_OUTPUT = DIST / "ZP-AC-2.1.7.mcaddon"
WHEEL_PATTERN = "endstone_zp_report_anticheat-0.6.8-*.whl"


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    required = [
        ADDON_DIR / "manifest.json",
        ADDON_DIR / "scripts" / "main.js",
        ROOT / "plugins" / "endstone_zp_anticheat" / "plugin.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "policy.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "credit.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "movement.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "config.toml",
        ROOT / "plugins" / "endstone_zp_anticheat" / "ai" / "engine.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "__init__.py",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Missing release input(s): " + ", ".join(missing))

    # compileall in CI must not leak __pycache__ files into the distributable wheel.
    for cache_dir in ROOT.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
    for bytecode in ROOT.rglob("*.pyc"):
        try:
            bytecode.unlink()
        except FileNotFoundError:
            pass

    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(DIST)],
        cwd=ROOT,
        check=True,
    )

    with zipfile.ZipFile(ADDON_OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.write(ADDON_DIR / "manifest.json", "manifest.json")
        archive.write(ADDON_DIR / "scripts" / "main.js", "scripts/main.js")

    wheels = sorted(DIST.glob(WHEEL_PATTERN))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one 0.6.8 wheel, found {len(wheels)}")

    print(f"Created: {ADDON_OUTPUT.relative_to(ROOT)}")
    print(f"Created: {wheels[0].relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
