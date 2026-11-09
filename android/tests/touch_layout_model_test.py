#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Compile real F01 Java model on a host JDK, without Android SDK or downloads."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[2]
PACKAGE = Path("android/app/src/main/java/org/s2arm/android")


def run(source_root, negative_controls):
    javac = shutil.which("javac")
    if not javac:
        raise RuntimeError("javac is required (JDK 11+); no dependencies are downloaded")
    java = Path(javac).with_name("java.exe" if os.name == "nt" else "java")
    if not java.is_file():
        raise RuntimeError("java must be present next to javac")
    with tempfile.TemporaryDirectory(prefix="s2a-touch-layout-") as temp:
        root = Path(temp)
        model = source_root / PACKAGE / "TouchLayout.java"
        sources = [source_root / PACKAGE / "Json.java", model,
                   REPO / "android/tests/TouchLayoutTest.java"]
        fixture = REPO / "ios/layouts/classic-reference.json"

        def compile_run(files, output):
            output.mkdir()
            subprocess.run([javac, "--release", "11", "-encoding", "UTF-8", "-Xlint:all", "-Werror", "-d", str(output),
                            *map(str, files)], check=True, timeout=60)
            return subprocess.run([str(java), "-cp", str(output), "org.s2arm.android.TouchLayoutTest",
                                   str(fixture), str(output / "edited.json")], text=True, capture_output=True, timeout=30)

        result = compile_run(sources, root / "positive")
        print(result.stdout, end="")
        if result.returncode:
            raise RuntimeError(result.stderr)
        # Independent consumer: compare Java's export to the iOS fixture plus the specified edit.
        expected = json.loads(fixture.read_text(encoding="utf-8"))
        expected.update(id="user-layout", name='布局"\\测试😀')
        next(b for b in expected["buttons"] if b["id"] == "A").update(
            x=0.99, y=0.01, width=0.2, height=0.1, cornerRadius=0.05)
        actual = json.loads((root / "positive/edited.json").read_text(encoding="utf-8"))
        if actual != expected:
            raise RuntimeError("Java export differs from independently edited iOS reference")
        print("Python JSON consumer: exported document equals iOS reference + specified edit")

        if not negative_controls:
            return
        code = model.read_text(encoding="utf-8")
        mutations = [
            ("stick-diameter", "double diameter = s.radius * 2;", "double diameter = s.radius;",
             "stick landscape x"),
            ("overlap", "overlapWidth > TOLERANCE && overlapHeight > TOLERANCE", "false",
             "accepted edit overlapping sticks"),
            ("unknown-fields", "!object.keySet().equals(expected)", "false",
             "accepted unknown top field"),
            ("drag-clamp", "clamp01((p[0] - left) / width)", "(p[0] - left) / width",
             "at org.s2arm.android.TouchLayout.withButtonCenters"),
            ("hit-slop", "px >= f.x - slop && px < f.x + f.width + slop", "px >= f.x && px < f.x + f.width",
             "slop grows A frame"),
        ]
        for name, old, new, expected in mutations:
            if code.count(old) != 1:
                raise RuntimeError("negative control anchor changed: " + name)
            directory = root / name
            directory.mkdir()
            mutated = directory / "TouchLayout.java"
            mutated.write_text(code.replace(old, new), encoding="utf-8")
            result = compile_run([sources[0], mutated, sources[2]], directory / "classes")
            if result.returncode == 0 or expected not in result.stderr:
                raise RuntimeError("negative control did not fail at intended assertion: " + name
                                   + "\n" + result.stdout + result.stderr)
            print("negative control " + name + ": RED (" + expected + ")")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=REPO)
    parser.add_argument("--negative-controls", action="store_true")
    options = parser.parse_args()
    run(options.source_root, options.negative_controls)
