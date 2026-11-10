from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ADDON_DIR = ROOT / "mcaddon"
RESOURCE_DIR = ROOT / "resource_pack"
ADDON_OUTPUT = DIST / "ZP-AC-2.1.9.mcaddon"
WHEEL_PATTERN = "endstone_zp_report_anticheat-1.0.0-*.whl"

BP_PREFIX = "behavior_pack/"
RP_PREFIX = "resource_pack/"


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise SystemExit(f"Invalid PNG file: {path.relative_to(ROOT)}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise SystemExit(f"Invalid PNG dimensions: {path.relative_to(ROOT)}")
    return width, height


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    required = [
        ADDON_DIR / "manifest.json",
        ADDON_DIR / "scripts" / "main.js",
        RESOURCE_DIR / "manifest.json",
        RESOURCE_DIR / "pack_icon.png",
        RESOURCE_DIR / "textures" / "ui" / "zp_ac_logo.png",
        RESOURCE_DIR / "textures" / "ui" / "icon_shield.png",
        RESOURCE_DIR / "textures" / "ui" / "icon_credit.png",
        RESOURCE_DIR / "textures" / "ui" / "icon_report.png",
        RESOURCE_DIR / "textures" / "ui" / "menu_panel.png",
        ROOT / "plugins" / "endstone_zp_anticheat" / "plugin.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "mute.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "chat_moderation.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "client_security.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "client_bridge.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "policy.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "credit.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "movement.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "evidence_store.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "runtime_metrics.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "mining_analysis.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "config.toml",
        ROOT / "plugins" / "endstone_zp_anticheat" / "ai" / "engine.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "ai" / "onnx_engine.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "ai" / "dataset.py",
        ROOT / "plugins" / "endstone_zp_anticheat" / "__init__.py",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Missing release input(s): " + ", ".join(missing))

    bp_manifest = json.loads((ADDON_DIR / "manifest.json").read_text(encoding="utf-8"))
    rp_manifest = json.loads((RESOURCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    rp_uuid = rp_manifest["header"]["uuid"]
    if not any(dep.get("uuid") == rp_uuid and dep.get("version") == rp_manifest["header"]["version"]
               for dep in bp_manifest.get("dependencies", [])):
        raise SystemExit("Behavior pack does not declare the matching resource-pack dependency.")

    png_files = [RESOURCE_DIR / "pack_icon.png", *sorted((RESOURCE_DIR / "textures").rglob("*.png"))]
    for png in png_files:
        print(f"PNG OK {png.relative_to(ROOT)}: {png_size(png)}")

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

    # A .mcaddon is a ZIP containing two sibling pack directories. Each
    # directory must have its own manifest at its root.
    with zipfile.ZipFile(ADDON_OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, prefix in ((ADDON_DIR, BP_PREFIX), (RESOURCE_DIR, RP_PREFIX)):
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    archive.write(path, prefix + path.relative_to(source).as_posix())

    with zipfile.ZipFile(ADDON_OUTPUT) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise SystemExit(f"Corrupt ZIP member: {bad_member}")
        names = set(archive.namelist())
        expected = {
            BP_PREFIX + "manifest.json",
            BP_PREFIX + "scripts/main.js",
            RP_PREFIX + "manifest.json",
            RP_PREFIX + "pack_icon.png",
            RP_PREFIX + "textures/ui/zp_ac_logo.png",
            RP_PREFIX + "textures/ui/icon_shield.png",
            RP_PREFIX + "textures/ui/icon_credit.png",
            RP_PREFIX + "textures/ui/icon_report.png",
            RP_PREFIX + "textures/ui/menu_panel.png",
        }
        missing_members = sorted(expected - names)
        if missing_members:
            raise SystemExit("Missing .mcaddon member(s): " + ", ".join(missing_members))
        if "manifest.json" in names or "scripts/main.js" in names:
            raise SystemExit("Pack files must be nested in behavior_pack/ and resource_pack/ folders.")
        packed_bp = json.loads(archive.read(BP_PREFIX + "manifest.json"))
        packed_rp = json.loads(archive.read(RP_PREFIX + "manifest.json"))
        if not any(dep.get("uuid") == packed_rp["header"]["uuid"] for dep in packed_bp.get("dependencies", [])):
            raise SystemExit("Packed behavior/resource pack UUID dependency mismatch.")

    wheels = sorted(DIST.glob(WHEEL_PATTERN))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one 1.0.0 wheel, found {len(wheels)}")

    print(f"Created and validated: {ADDON_OUTPUT.relative_to(ROOT)}")
    print(f"Created: {wheels[0].relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
