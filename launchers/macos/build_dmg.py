#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Package an existing signed S2Arm.app with the branded Finder installation window."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile

from frontend_layout import FRONTENDS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LAYOUT = json.loads((HERE / "Branding/dmg-layout.json").read_text())


def artwork(destination, app=None):
    destination.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # The compiler cache is a build artifact; keep it in the requested output tree.
    env["CLANG_MODULE_CACHE_PATH"] = str(destination / "module-cache")
    command = ["xcrun", "swift", str(HERE / "Branding/render_dmg.swift"), str(destination)]
    if app:
        command.append(str(app))
    subprocess.run(command, check=True, env=env)


def build(app, output):
    try:
        import dmgbuild
    except ImportError as error:
        raise SystemExit("请先安装打包依赖：python3 -m pip install -r launchers/macos/requirements-dmg.txt") from error

    app, output = app.resolve(), output.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    identities = {(front["bundle_id"], front["product"]) for front in FRONTENDS.values()}
    if app.name != "S2Arm.app" or (info.get("CFBundleIdentifier"), info.get("CFBundleExecutable")) not in identities:
        raise ValueError("请指定完整的 S2Arm.app")
    if app == output or app in output.parents:
        raise ValueError("DMG 输出不能放在应用包内")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".s2arm-dmg-", dir=output.parent) as directory:
        work = Path(directory)
        artwork(work, app)
        settings = {
            "format": "ULFO", "filesystem": "HFS+",
            "files": [str(app)], "symlinks": {"Applications": "/Applications"},
            "background": str(work / "background.tiff"),
            "window_rect": ((180, 160), (LAYOUT["width"], LAYOUT["height"] + 32)),
            "icon_locations": {name: tuple(point) for name, point in LAYOUT["iconPositions"].items()},
            "icon_size": LAYOUT["iconSize"], "text_size": 13, "label_pos": "bottom",
            "default_view": "icon-view", "include_icon_view_settings": True,
            "show_toolbar": False, "show_sidebar": False, "show_status_bar": False,
            "show_tab_view": False, "show_pathbar": False, "show_icon_preview": False,
            "arrange_by": None, "scroll_position": (0, 0),
            # Hiding .app adds FinderInfo to the signed bundle. Preserve its bytes.
            "hide_extensions": [],
            "create_hook": lambda mount, _: subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(Path(mount) / app.name)], check=True
            ),
        }
        icon = app / "Contents/Resources" / info.get("CFBundleIconFile", "S2Arm")
        if not icon.suffix:
            icon = icon.with_suffix(".icns")
        if icon.is_file():
            settings["icon"] = str(icon)
        temporary = work / "installer.dmg"
        dmgbuild.build_dmg(str(temporary), "S2Arm Installer", settings=settings)
        subprocess.run(["hdiutil", "verify", str(temporary)], check=True)
        os.replace(temporary, output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "build/macos/S2Arm.app")
    parser.add_argument("--output", type=Path, default=ROOT / "build/macos/S2Arm-mac-arm64.dmg")
    parser.add_argument("--preview", type=Path, help="仅生成背景和示意预览，不打包应用；不需要 dmgbuild")
    args = parser.parse_args()
    if args.preview:
        artwork(args.preview.resolve(), args.app.resolve() if args.app.is_dir() else None)
    else:
        print("Created:", build(args.app, args.output))


if __name__ == "__main__":
    main()
