#!/usr/bin/env python3
"""从源码构建 Mac 启动器的一键准备：检查 / 准备全部构建依赖，缺什么当场说清楚、给出装法。

    python3 launchers/macos/prepare_build.py [--venv <目录>] [--check]

查（只读，不替你装系统级东西）：Apple 芯片 macOS；Xcode（同出包的 tools/toolchain.sh 解析：S2A_DEVELOPER_DIR / tools/machine.local → xcode-select）
  与 iOS SDK、Metal 编译器——编译器版本与 translator/dmsh_chain.lock 不同只出 note（出包时直连库按本机编译器重编，不报错）；
  .NET SDK——在 compat/tree 里跑 dotnet --version，由 dotnet 按 compat/tree/global.json（版本 + rollForward）判定，不另抄版本号；
  Homebrew 公式（运行环境打包 bundle_runtime.py 收的工具 + C 改写器的 CMake + dav1d 的 meson / ninja）；Rust（pack/devpush）。
准备（可逆、只写在你给的目录或本树 build/ 下；--check 时跳过）：
  构建用 Python 虚拟环境（缺省本树 build/venv，Homebrew python@3.12 建）装齐三份钉版清单：tools/rewriter-requirements.txt、
  pack/fonts/requirements.txt（字体生成器 fonttools / skia-pathops）、launchers/macos/requirements-runtime.txt；
  C 改写器钉版源码（runtime/rewriter/prepare_deps.py → 本树 build/rewriter-deps；已有完整的一份就不下载）。
全部就绪退出 0，并打印下一步命令；有缺项退出 1。不读、不写开发机专用路径。"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
REQS = ["tools/rewriter-requirements.txt", "pack/fonts/requirements.txt", "launchers/macos/requirements-runtime.txt"]
# 公式 → 谁要：bundle_runtime.py 的 prefixes 全部 + C 改写器（runtime/rewriter，CMake 3.18+）+ services/dav1d/build.sh（meson + ninja）
FORMULAS = {"python@3.12": "构建 venv",
            "bash": "运行环境（bash 5）", "cmake": "C 改写器", "meson": "dav1d 静态库", "ninja": "dav1d 静态库"}
IMPORTS = "import capstone, lz4, numpy, PIL, fontTools, pathops"
rows = []


def row(name, ok, detail, fix=""):
    rows.append((name, ok, detail, fix))


def run(args, **kw):
    try:
        return subprocess.run([str(a) for a in args], capture_output=True, text=True, **kw)
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, "", str(e))


def check_xcode():
    p = run(["bash", "-c", 'S2A_TC_QUIET=1 source tools/toolchain.sh > /dev/null && printf "%s\\n%s\\n%s" "$DEVELOPER_DIR" "$S2A_TC_SDK" "$S2A_TC_METAL"'], cwd=ROOT)
    if p.returncode != 0:
        row("Xcode / Metal", False, (p.stderr.strip().splitlines() or ["tools/toolchain.sh 解析失败"])[-1].lstrip("🔴 "),
            "装完整 Xcode 并打开一次装 iOS 平台与 Metal Toolchain；装了多个时 sudo xcode-select -s <Xcode.app>/Contents/Developer 或设 S2A_DEVELOPER_DIR")
        return None
    dev, sdk, metal = (p.stdout.split("\n") + ["", "", ""])[:3]
    row("Xcode / Metal", True, f"{dev}；iOS SDK {sdk}；{metal}")
    lock = ROOT / "translator/dmsh_chain.lock"
    locked = next((l.split("\t", 1)[1].strip() for l in lock.read_text(encoding="utf-8").splitlines() if l.startswith("metal_iphoneos\t")), "") if lock.is_file() else ""
    if locked and locked != metal.strip():
        row("Metal 与锁", True, f"note：本机 {metal.strip()}，锁文件 {locked}。只出 note 不报错：直连库出库前核工具时放行（dmsh_chain.py --build-env），"
            "Metal 编译缓存按编译器身份分键，换 Xcode 只是重编")
    return dev


def find_dotnet():
    d = shutil.which("dotnet")
    if d:
        return d
    home = Path.home() / ".dotnet/dotnet"   # 微软 dotnet-install 的缺省位置（不是我方开发机路径）
    return str(home) if home.is_file() else None


def check_dotnet():
    want = (ROOT / "compat/tree/global.json").read_text(encoding="utf-8")
    fix = ("按 compat/tree/global.json 装官方 SDK：python3 pack/jobs/applenv.py --install dotnet，"
           "再把 ~/.dotnet 放进 PATH（或 python3 pack/jobs/applenv.py --install dotnet）")
    d = find_dotnet()
    if not d:
        row(".NET SDK", False, "没有 dotnet", fix)
        return
    p = run([d, "--version"], cwd=ROOT / "compat/tree")   # 在 global.json 所在目录跑：dotnet 自己按 version + rollForward 选 SDK，选不到就报错
    if p.returncode != 0:
        out = [l.strip() for l in (p.stderr + p.stdout).splitlines() if l.strip()]
        why = next((l for l in out if l.startswith(("A compatible .NET SDK", "Requested SDK"))), out[0] if out else f"退出码 {p.returncode}")
        have = run([d, "--list-sdks"]).stdout.split()[::2]   # 「10.0.100 [路径]」→ 版本号
        row(".NET SDK", False, f"{d} 没有满足 global.json（{' '.join(want.split())}）的 SDK：{why}；本机已装 {'、'.join(have) or '无'}", fix)
        return
    real = os.path.realpath(d)
    if "/Cellar/" in real or "/homebrew/" in real:   # 同 docs/贡献者开发测试套件.md：源码构建版缺 Brotli 静态库，编 Mac 版 C# 库报 _Brotli* 未定义
        row(".NET SDK", False, f"{p.stdout.strip()}（{real}）是 Homebrew 源码构建版，缺 Brotli 静态库，编 Mac 版 C# 库会报 _Brotli* 未定义", fix)
        return
    row(".NET SDK", True, f"{p.stdout.strip()}（{d}，按 compat/tree/global.json 选中）")


def check_brew():
    brew = shutil.which("brew")
    if not brew:
        row("Homebrew", False, "没有 brew", "按 https://brew.sh 安装 Homebrew")
        return None
    missing = [f for f in FORMULAS if run([brew, "list", "--versions", f]).returncode != 0]
    if missing:
        row("Homebrew 公式", False, "缺 " + "、".join(f"{f}（{FORMULAS[f]}）" for f in missing), "brew install " + " ".join(missing))
    else:
        row("Homebrew 公式", True, " ".join(FORMULAS))
    return brew


def check_cargo():
    c = shutil.which("cargo") or (str(Path.home() / ".cargo/bin/cargo") if (Path.home() / ".cargo/bin/cargo").is_file() else None)   # rustup 的缺省位置，同 bundle_engine.py
    row("Rust（cargo）", bool(c), c or "没有 cargo（pack/devpush 推资源包工具要编）", "" if c else "按 https://rustup.rs 安装 Rust")


def prepare_venv(venv, brew, check_only):
    py = venv / "bin/python3"
    base = Path(run([brew, "--prefix", "python@3.12"]).stdout.strip() or "/nonexistent") / "bin/python3.12" if brew else None
    if not py.is_file():
        if check_only:
            row("构建 venv", False, f"{venv} 还没建", f"python3 {Path(__file__).relative_to(ROOT)} --venv {venv}")
            return None
        if not base or not base.is_file():
            row("构建 venv", False, "没有 Homebrew python@3.12，建不了构建 venv", "brew install python@3.12")
            return None
        p = run([base, "-m", "venv", venv])
        if p.returncode != 0:
            row("构建 venv", False, f"建 {venv} 失败：{p.stderr.strip()[-200:]}")
            return None
    if not check_only:
        args = [py, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
        for r in REQS:
            args += ["-r", ROOT / r]
        p = run(args)
        if p.returncode != 0:
            row("构建 venv", False, f"装钉版依赖失败：{(p.stderr or p.stdout).strip()[-300:]}", f"{py} -m pip install " + " ".join(f"-r {r}" for r in REQS))
            return None
    p = run([py, "-c", IMPORTS + "; import sys; print(sys.version.split()[0])"])
    if p.returncode != 0:
        row("构建 venv", False, f"{py} 缺包：{p.stderr.strip().splitlines()[-1] if p.stderr.strip() else ''}", f"{py} -m pip install " + " ".join(f"-r {r}" for r in REQS))
        return None
    row("构建 venv", True, f"{py}（Python {p.stdout.strip()}；{'、'.join(REQS)} 已装齐）")
    return py


def prepare_rewriter_deps(check_only):
    sys.path.insert(0, str(ROOT))
    from pack import rewriter
    try:
        row("C 改写器固定依赖", True, str(rewriter.dependency_dir()))
        return
    except ValueError as e:
        message = str(e)
    target = ROOT / "build/rewriter-deps"
    if check_only:
        row("C 改写器固定依赖", False, message, f"python3 runtime/rewriter/prepare_deps.py {target.relative_to(ROOT)}")
        return
    if any(target.glob("*-*/")):   # prepare_deps.py 不覆盖已解开的目录：半截的要先挪走
        row("C 改写器固定依赖", False, f"{target} 里有不完整的旧内容", f"删掉 {target.relative_to(ROOT)} 后重跑本命令")
        return
    p = run([sys.executable, ROOT / "runtime/rewriter/prepare_deps.py", target])
    try:
        row("C 改写器固定依赖", p.returncode == 0, str(rewriter.dependency_dir()) if p.returncode == 0 else (p.stderr or p.stdout).strip()[-300:])
    except ValueError as e:
        row("C 改写器固定依赖", False, str(e))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venv", type=Path, default=ROOT / "build/venv", help="构建用 Python 虚拟环境目录（缺省本树 build/venv）")
    ap.add_argument("--check", action="store_true", help="只检查，不建 venv、不下载")
    a = ap.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        print("🔴 从源码构建 Mac 启动器要 Apple 芯片的 macOS（运行环境只收 arm64）；Intel Mac 只能用 pack/oneclick/oneclick.sh 命令行出 iPhone 包")
        return 1
    check_xcode()
    check_dotnet()
    brew = check_brew()
    check_cargo()
    venv = a.venv.expanduser().resolve()
    py = prepare_venv(venv, brew, a.check)
    prepare_rewriter_deps(a.check)
    print()
    for name, ok, detail, fix in rows:
        print(f"{'✓' if ok else '✗'} {name}：{detail}")
        if fix and not ok:
            print("    → " + fix)
    bad = [r for r in rows if not r[1]]
    if bad:
        print(f"\n🔴 {len(bad)} 项没就绪：按上面「→」那行处理后重跑本命令")
        return 1
    print("\n✓ 构建依赖齐了。下一步：")
    print(f"  {py} launchers/macos/build_app.py        # 产物 build/macos/S2Arm.app（字体生成器与运行环境打包都用这个解释器）")
    print(f"  命令行出包 / 跑测试前：export S2A_PY={py}   （或写进 tools/machine.local：S2A_PY={py}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
