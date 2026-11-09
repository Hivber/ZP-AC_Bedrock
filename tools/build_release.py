from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BP_DIR = ROOT / "mcaddon"
RP_DIR = ROOT / "resource_pack"
ADDON_OUTPUT = DIST / "ZP-AC-2.1.9.mcaddon"
WHEEL_PATTERN = "endstone_zp_report_anticheat-0.6.9-*.whl"


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    required = [
        BP_DIR / "manifest.json",
        BP_DIR / "scripts" / "main.js",
        RP_DIR / "manifest.json",
        RP_DIR / "pack_icon.png",
        RP_DIR / "textures" / "ui" / "zp_logo.png",
        RP_DIR / "textures" / "ui" / "anticheat_shield.png",
        RP_DIR / "textures" / "ui" / "credit.png",
        RP_DIR / "textures" / "ui" / "report.png",
        RP_DIR / "textures" / "ui" / "menu.png",
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

    # A single .mcaddon contains two independently manifested packs.
    with zipfile.ZipFile(ADDON_OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in BP_DIR.rglob("*"):
            if source.is_file():
                archive.write(source, Path("behavior_pack") / source.relative_to(BP_DIR))
        for source in RP_DIR.rglob("*"):
            if source.is_file():
                archive.write(source, Path("resource_pack") / source.relative_to(RP_DIR))

    with zipfile.ZipFile(ADDON_OUTPUT) as archive:
        names = set(archive.namelist())
        assert "behavior_pack/manifest.json" in names
        assert "behavior_pack/scripts/main.js" in names
        assert "resource_pack/manifest.json" in names
        assert "resource_pack/pack_icon.png" in names
        assert "resource_pack/textures/ui/zp_logo.png" in names

    wheels = sorted(DIST.glob(WHEEL_PATTERN))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one 0.6.9 wheel, found {len(wheels)}")

    print(f"Created: {ADDON_OUTPUT.relative_to(ROOT)}")
    print(f"Created: {wheels[0].relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
