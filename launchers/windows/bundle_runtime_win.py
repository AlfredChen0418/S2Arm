#!/usr/bin/env python3
"""Windows 启动器随包运行时（启动器第 2 步，11-06 维护者：玩家「几乎不用额外装依赖」；同 Mac launchers/macos/bundle_runtime.py 的口径）。
在 Windows 出包机上：python -X utf8 launchers/windows/bundle_runtime_win.py <目标>   → <目标>/Runtime/（不许已存在：不混旧件）
  python/        官方 Python 3.12 嵌入版（专为随应用分发）+ 钉版 capstone / lz4 / numpy / Pillow / zstandard（同 launchers/macos/requirements-runtime.txt）
  dotnet/        .NET 10 运行时（dotnet.exe + host + shared/Microsoft.NETCore.App/<10.x>，不带 SDK：C# 件发版预编，见 bundle_prepare_win.py）
  git/usr/bin/   Git for Windows 的 usr/bin（bash 与 awk / sed / cut / join 等，直连库等出包脚本在 bash 下跑）；独立进程调用，不与我们的代码链接
  zstd/          官方 zstd-v1.5.7-win64（布局阶段钉版命令行 + libzstd）
  mingit/        官方 MinGit（同本机 Git for Windows 版本）：壳号 / 源码身份按 git 对象号算（pack/shell/manifest.py 等），引擎快照同 Mac 是 git 仓库
  licenses/      各件许可原文 + 源码获取说明（GPL 件）
  s2arm_env.cmd  出包作业的环境（PATH / DOTNET_ROOT / S2A_BASH）：启动器起作业前 call 它，并设 S2A_ENGINE=<引擎目录>（随包 Python 按它找 pack.*）
  versions.json  各件版本与来源
来源：Python 嵌入版从 python.org 官方地址下载；.NET 运行时、Git、zstd 从本机已装的官方发行版拷（路径见下面常量，可用环境变量改）。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PY_VER = "3.12.10"
PY_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
DOTNET = Path(os.environ.get("S2A_RT_DOTNET", r"C:\Program Files\dotnet"))
GIT = Path(os.environ.get("S2A_RT_GIT", r"C:\Program Files\Git"))
ZSTD = Path(os.environ.get("S2A_RT_ZSTD", r"R:\s2a\deps\zstd\zstd-v1.5.7-win64"))
GPL_OFFER = """S2Arm 的 Windows 运行时把 Git for Windows 的 usr/bin（GNU bash、coreutils、gawk、sed、grep 等 MSYS2 程序与 msys-2.0.dll 运行库）
作为独立程序放在 Runtime/git/usr/bin。它们由出包作业经 PATH 单独拉起，不与 S2Arm 的代码静态链接，也不作为 S2Arm 的动态库加载。

许可：git GPL-2.0；bash、coreutils、gawk、sed、grep 等 GPL-3.0-or-later；MSYS2 运行库 GPL-3.0（带链接例外）。许可全文见同目录 git-*.txt。
对应版本：Runtime/versions.json 里的 git 字段（Git for Windows 版本）。
对应源码：https://github.com/git-for-windows/git/releases 下与该版本一致的发行说明列出的各组件源码包；
  MSYS2 程序源码：https://github.com/msys2/MSYS2-packages （按 git-for-windows/build-extra 记录的版本）。

书面提供：自收到书面请求起，按 GPL 第 6 节提供上述对应版本的源码。请求请发到 https://github.com/jhaizhou-ops/S2Arm/issues （面向维护者）。

本说明只覆盖上述独立程序，不把 S2Arm 其余代码改为 GPL。
"""


def say(m): print(m, flush=True)


def pins():
    req = (ROOT / "launchers/macos/requirements-runtime.txt").read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in req if "==" in l and not l.startswith("#")]


def python(rt, versions):
    d = rt / "python"; d.mkdir()
    with tempfile.TemporaryDirectory() as t:
        z = Path(t, "py.zip"); urllib.request.urlretrieve(PY_URL, z)
        zipfile.ZipFile(z).extractall(d)
    # 去掉嵌入版的 ._pth：它固定模块路径、连「被运行脚本所在目录」都不加（仓库里大量 python <目录>/x.py 导入同目录兄弟模块，11-06 验收
    #   spvc_inproc.py 找不到 xlock）。改由 s2arm_env.cmd 设 PYTHONHOME=<本目录>（不读注册表里机器另装的 Python）+ PYTHONNOUSERSITE=1（不读用户 site-packages）
    for pth in d.glob("python3*._pth"): pth.unlink()
    site = d / "Lib/site-packages"; site.mkdir(parents=True)
    # 按环境变量 S2A_ENGINE（启动器设成引擎目录）把引擎根插到模块路径最前：python -m pack.… 不依赖当前目录
    (site / "s2arm_engine.pth").write_text("import os, sys; p = os.environ.get('S2A_ENGINE'); p and sys.path.insert(0, p)\n", encoding="utf-8")
    # 本机 pip 按目标平台取官方二进制轮子装进 site-packages（不编译）
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "--only-binary=:all:",
                    "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp", "--target", str(site), *pins()], check=True)
    versions["python"] = PY_VER; versions["python_url"] = PY_URL; versions["python_packages"] = pins()


def dotnet(rt, versions):
    d = rt / "dotnet"
    have = sorted((DOTNET / "shared/Microsoft.NETCore.App").glob("10.*"), key=lambda p: tuple(int(x) for x in p.name.split(".")))
    if not have: raise SystemExit(f"【红】{DOTNET} 下没有 .NET 10 运行时")
    shutil.copytree(have[-1], d / "shared/Microsoft.NETCore.App" / have[-1].name)
    shutil.copytree(DOTNET / "host", d / "host")
    shutil.copy2(DOTNET / "dotnet.exe", d / "dotnet.exe")
    versions["dotnet"] = have[-1].name


def git(rt, versions):
    shutil.copytree(GIT / "usr/bin", rt / "git/usr/bin")
    if (GIT / "etc").is_dir(): shutil.copytree(GIT / "etc", rt / "git/etc")   # bash 读 /etc 下的 profile / passwd 等（缺了照跑，带上少告警）
    out = subprocess.run([str(GIT / "cmd/git.exe"), "--version"], capture_output=True, text=True).stdout.strip()
    versions["git"] = out.replace("git version ", "")


ZSTD_LICENSE = "https://raw.githubusercontent.com/facebook/zstd/v1.5.7/LICENSE"   # 官方发行包 zstd-v1.5.7-win64 不带 LICENSE，按同一标签取原文


def mingit(rt, versions):
    ver = versions["git"]   # 例 2.54.0.windows.1
    url = f"https://github.com/git-for-windows/git/releases/download/v{ver}/MinGit-{ver.split('.windows')[0]}-64-bit.zip"
    d = rt / "mingit"; d.mkdir()
    with tempfile.TemporaryDirectory() as t:
        z = Path(t, "mingit.zip"); urllib.request.urlretrieve(url, z)
        zipfile.ZipFile(z).extractall(d)
    versions["mingit_url"] = url


def zstd(rt, versions):
    shutil.copytree(ZSTD, rt / "zstd")
    if not (rt / "zstd/LICENSE").is_file(): urllib.request.urlretrieve(ZSTD_LICENSE, rt / "zstd/LICENSE")
    versions["zstd"] = subprocess.run([str(rt / "zstd/zstd.exe"), "--version"], capture_output=True, text=True).stdout.strip()


def licenses(rt):
    L = rt / "licenses"; L.mkdir()
    pairs = [(rt / "python/LICENSE.txt", "Python-LICENSE.txt"), (DOTNET / "LICENSE.txt", "dotnet-LICENSE.txt"),
             (DOTNET / "ThirdPartyNotices.txt", "dotnet-ThirdPartyNotices.txt"), (GIT / "LICENSE.txt", "git-LICENSE.txt"),
             (rt / "zstd/LICENSE", "Zstandard-LICENSE.txt"),
             (ROOT / "runtime/rewriter/LICENSES/capstone.txt", "capstone-LICENSE.txt"), (ROOT / "runtime/rewriter/LICENSES/lz4.txt", "lz4-LICENSE.txt")]
    for src, name in pairs:
        if not src.is_file(): raise SystemExit(f"【红】缺许可原文：{src}")
        shutil.copy2(src, L / name)
    for name in ("COPYING", "COPYING.LIB"):   # MSYS2 / GNU 程序的 GPL 原文（Git for Windows 放在 usr/share/licenses 或根下，有就带）
        for p in list((GIT / "usr/share/licenses").glob(f"*/{name}"))[:1] if (GIT / "usr/share/licenses").is_dir() else []:
            shutil.copy2(p, L / f"git-{name}.txt")
    (L / "S2Arm-runtime-source-offer.txt").write_text(GPL_OFFER, encoding="utf-8")   # numpy / Pillow 等轮子的许可在各自 *.dist-info 里，随包保留


def env_cmd(rt):
    (rt / "s2arm_env.cmd").write_bytes(("\r\n".join([
        "@echo off",
        "rem S2Arm 出包作业环境（bundle_runtime_win.py 生成）：只用随包运行时，不依赖玩家机上另装的 Python / Git / .NET",
        'set "S2A_RT=%~dp0"',
        'set "PATH=%S2A_RT%python;%S2A_RT%python\\Scripts;%S2A_RT%dotnet;%S2A_RT%mingit\\cmd;%S2A_RT%git\\usr\\bin;%S2A_RT%zstd;%SystemRoot%\\system32;%SystemRoot%;%SystemRoot%\\System32\\WindowsPowerShell\\v1.0"',
        'set "DOTNET_ROOT=%S2A_RT%dotnet"', 'set "DOTNET_CLI_TELEMETRY_OPTOUT=1"',
        'set "S2A_BASH=%S2A_RT%git\\usr\\bin\\bash.exe"', 'set "PYTHONUTF8=1"', 'set "LC_ALL=C.UTF-8"', 'set "PYTHONDONTWRITEBYTECODE=1"',
        'set "PYTHONHOME=%S2A_RT%python"', 'set "PYTHONNOUSERSITE=1"', 'set "PYTHONPATH="', ""]) ).encode("utf-8"))


def smoke(rt):   # 每件跑一下：Python 带四个库、dotnet 只有运行时、bash 跑管道、zstd 版本
    env = dict(os.environ, PATH=os.pathsep.join([str(rt / "python"), str(rt / "dotnet"), str(rt / "git/usr/bin"), str(rt / "zstd"), os.environ.get("SystemRoot", r"C:\Windows") + r"\system32"]))
    checks = [([str(rt / "python/python.exe"), "-B", "-c", "import capstone, lz4.block, numpy, PIL.Image, zstandard, ctypes, zlib, hashlib, multiprocessing; print('python ok', numpy.__version__)"], "python ok"),
              ([str(rt / "dotnet/dotnet.exe"), "--list-runtimes"], "Microsoft.NETCore.App 10."),
              ([str(rt / "git/usr/bin/bash.exe"), "-c", "printf 'b\\na\\n' | sort | head -1 | awk '{print \"bash \" $1}'"], "bash a"),
              ([str(rt / "zstd/zstd.exe"), "--version"], "v1.5.7"),
              ([str(rt / "mingit/cmd/git.exe"), "--version"], "git version")]
    env.update(PYTHONHOME=str(rt / "python"), PYTHONNOUSERSITE="1"); env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory() as t:   # 脚本能导入同目录兄弟模块；模块路径只含随包 python 目录（不混机器上另装的 Python / 用户 site-packages）
        Path(t, "sib.py").write_text("X = 1\n", encoding="utf-8")
        Path(t, "main.py").write_text("import sys, sib\nbad = [p for p in sys.path[1:] if p and not p.lower().startswith(sys.argv[1].lower())]\nprint('pathok' if not bad else 'bad ' + repr(bad))\n", encoding="utf-8")
        checks.append(([str(rt / "python/python.exe"), str(Path(t, "main.py")), str(rt / "python")], "pathok"))
        for cmd, want in checks:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if want not in p.stdout: raise SystemExit(f"【红】自检不过：{' '.join(cmd)[:120]} → {(p.stdout + p.stderr).strip()[-300:]}")
            say(f"  自检 {Path(cmd[-1] if cmd[-1].endswith('python') else cmd[0]).name}：{p.stdout.strip().splitlines()[0][:80]}")
    if subprocess.run([str(rt / "dotnet/dotnet.exe"), "--list-sdks"], capture_output=True, text=True, env=env).stdout.strip():
        raise SystemExit("【红】随包 dotnet 带了 SDK（只该带运行时）")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0].startswith("-"): raise SystemExit("用法：bundle_runtime_win.py <目标目录>")
    rt = Path(argv[0]) / "Runtime"
    if rt.exists(): raise SystemExit(f"【红】{rt} 已存在，不混旧件")
    rt.mkdir(parents=True); versions = {}
    for step in (python, dotnet, git, mingit, zstd):
        say(f"[运行时] {step.__name__}"); step(rt, versions)
    licenses(rt); env_cmd(rt)
    (rt / "versions.json").write_text(json.dumps(versions, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    smoke(rt)
    size = sum(p.stat().st_size for p in rt.rglob("*") if p.is_file())
    say(f"🟢 运行时 → {rt}（{size / 2**20:.0f} MB）：{json.dumps(versions, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
