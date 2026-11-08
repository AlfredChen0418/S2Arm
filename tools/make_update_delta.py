#!/usr/bin/env python3
"""发版时生成覆盖更新的差分包，并传到 GitHub Release（合入运维用）。

    python3 tools/make_update_delta.py --from v0.1.1 --to v0.1.2

从 Release 下载两个版本的 Mac / Windows 整包，按文件算差分，把差分包传到 --to 的 Release，并把校验值写进 SHA256SUMS.txt。
Mac 包要在 Mac 上解（hdiutil）。两个版本都要有整包，--to 的 SHA256SUMS.txt 要已经存在。
传完后要重跑 tools/sign_update_manifest.py --tag <--to>，启动器只认签名清单里的文件。
"""
import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pack/jobs"))
import updater  # noqa: E402

REPO = "jhaizhou-ops/S2Arm"
SUMS = "SHA256SUMS.txt"


def gh(*args):
    subprocess.run(["gh", *args], check=True)


def fetch(tag, asset, folder):
    gh("release", "download", tag, "--repo", REPO, "--pattern", asset, "--dir", str(folder), "--clobber")
    return folder / asset


def unpack(platform, package, folder):
    folder.mkdir()
    return updater.extract_mac(package, folder) if platform == "mac" else updater.extract_windows(package, folder)


def plain(tag):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit(f"版本要写成 vX.Y.Z：{tag}")
    return tag[1:]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="source", required=True); ap.add_argument("--to", dest="target", required=True)
    ap.add_argument("--no-upload", action="store_true", help="只生成到 --out，不传 Release")
    ap.add_argument("--out", default="build/update_delta")
    a = ap.parse_args(argv)
    source, target = plain(a.source), plain(a.target)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    made = []
    with tempfile.TemporaryDirectory(prefix="s2arm-delta-") as tmp:
        for platform, asset in updater.ASSETS.items():
            work = Path(tmp) / platform; work.mkdir()
            (work / "old").mkdir(); (work / "new").mkdir()
            old = unpack(platform, fetch(a.source, asset, work / "old"), work / "old/x")
            new = unpack(platform, fetch(a.target, asset, work / "new"), work / "new/x")
            made.append(updater.build_delta(old, new, platform, source, target, out))
            print(f"{made[-1].name}：{made[-1].stat().st_size / 2**20:.1f} MB")
    if a.no_upload:
        return 0
    sums = fetch(a.target, SUMS, out)
    lines = [line for line in sums.read_text().splitlines() if not any(line.endswith(p.name) for p in made)]
    lines += [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in made]
    sums.write_text("\n".join(lines) + "\n")
    gh("release", "upload", a.target, "--repo", REPO, "--clobber", *map(str, made), str(sums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
