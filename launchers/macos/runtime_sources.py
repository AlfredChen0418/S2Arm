#!/usr/bin/env python3
"""Mac 随包 Python 和 SPIRV 工具的钉版来源。

Homebrew 瓶子跟着出包机的系统走，在 macOS 26 上取到的程序最低系统也是 26，而 App 声明支持 15。
这里改用 python-build-standalone（官方构建，最低 macOS 11）和按 tools/toolchain.env 的部署目标静态编译的 SPIRV 工具。
下载校验 sha256，产物按版本和部署目标缓存在 build/macos/runtime-sources。
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
from hashlib import sha256

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "build/macos/runtime-sources"
SPIRV_TOOLS_VERSION, SPIRV_CROSS_VERSION, GLSLANG_VERSION, ZSTD_VERSION = "1.4.357.0", "1.4.363.0", "16.6.0", "1.5.7"
PYTHON = dict(url="https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz",
              sha256="353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e")
SOURCES = {
    "zstd": dict(url=f"https://github.com/facebook/zstd/releases/download/v{ZSTD_VERSION}/zstd-{ZSTD_VERSION}.tar.gz",
                 sha256="eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3"),
    "spirv-headers": dict(url=f"https://github.com/KhronosGroup/SPIRV-Headers/archive/refs/tags/vulkan-sdk-{SPIRV_TOOLS_VERSION}.tar.gz",
                          sha256="4d703067a7e06331ccb37bdfed3f9b7879cc61969a2689ae95c95db34a47ff07"),
    "spirv-tools": dict(url=f"https://github.com/KhronosGroup/SPIRV-Tools/archive/refs/tags/vulkan-sdk-{SPIRV_TOOLS_VERSION}.tar.gz",
                        sha256="d31e7109b6ef3559067e53e520870eafed7c9534d00db9728814b6df03fa4a5e"),
    "glslang": dict(url=f"https://github.com/KhronosGroup/glslang/archive/refs/tags/{GLSLANG_VERSION}.tar.gz",
                    sha256="9c09b901149c729df745057dafa815278aaa101b84d2b6e14f16a42de52f97f2"),
    "spirv-cross": dict(url=f"https://github.com/KhronosGroup/SPIRV-Cross/archive/refs/tags/vulkan-sdk-{SPIRV_CROSS_VERSION}.tar.gz",
                        sha256="f708982e88b763ef5b0394ed468fc2c3628f68cdab89f2e7a946cd628c04e721"),
}


def deployment_target():
    """App 声明的最低 macOS，唯一来源是 tools/toolchain.env。"""
    match = re.search(r"^MACOSX_DEPLOYMENT_TARGET=(\d+(?:\.\d+)*)\s*$", (ROOT / "tools/toolchain.env").read_text(), re.M)
    if not match:
        raise RuntimeError("tools/toolchain.env has no MACOSX_DEPLOYMENT_TARGET")
    return match[1]


def _download(name, spec):
    target = CACHE / "downloads" / (name + ".tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    def good():
        return target.is_file() and sha256(target.read_bytes()).hexdigest() == spec["sha256"]
    if not good():
        target.unlink(missing_ok=True)
        subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-all-errors", "-o", str(target), spec["url"]], check=True)
    if not good():
        raise RuntimeError(f"{name}: sha256 does not match the pinned value")
    return target


def _unpack(name, spec, dest):
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(_download(name, spec)) as archive:
        archive.extractall(dest, filter="data")
    return next(p for p in dest.iterdir() if p.is_dir())


def python():
    """解开的独立 Python 目录（bin/python3.12、lib/python3.12）。"""
    stamp, dest = CACHE / "python.stamp", CACHE / "python"
    if stamp.is_file() and stamp.read_text() == PYTHON["sha256"] and (dest / "python/bin/python3.12").is_file():
        return dest / "python"
    root = _unpack("python", PYTHON, dest)
    stamp.write_text(PYTHON["sha256"])
    return root


def _cmake(source, build, prefix, target, *options):
    os.environ["GIT_CEILING_DIRECTORIES"] = str(CACHE.parent)   # 源码包解在本仓库 build/ 下：别让 SPIRV 工具把本仓库的 git 版本号写进程序
    subprocess.run(["cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_INSTALL_PREFIX={prefix}",
                    f"-DCMAKE_OSX_DEPLOYMENT_TARGET={target}", "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_PREFIX_PATH={prefix}",
                    "-DBUILD_SHARED_LIBS=OFF", *options], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", str(build), "--parallel", "6"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--install", str(build)], check=True, stdout=subprocess.DEVNULL)


def tools():
    """静态编译 SPIRV 工具，返回 (安装前缀, {名字: 源码目录})。"""
    target = deployment_target()
    key = json.dumps(dict(target=target, sources={k: v["sha256"] for k, v in SOURCES.items()}), sort_keys=True)
    stamp, source_root, prefix, build = CACHE / "tools.stamp", CACHE / "src", CACHE / "tools", CACHE / "build"
    folders = {name: next((p for p in (source_root / name).iterdir() if p.is_dir()), None) for name in SOURCES} if stamp.is_file() and stamp.read_text() == key else None
    if folders and all(folders.values()) and all((prefix / "bin" / n).is_file() for n in ("spirv-as", "spirv-val", "glslangValidator", "spirv-cross", "zstd")):
        return prefix, folders
    for path in (stamp, ):
        path.unlink(missing_ok=True)
    shutil.rmtree(prefix, ignore_errors=True); shutil.rmtree(build, ignore_errors=True)
    folders = {name: _unpack(name, spec, source_root / name) for name, spec in SOURCES.items()}
    _cmake(folders["spirv-headers"], build / "headers", prefix, target)
    _cmake(folders["spirv-tools"], build / "tools", prefix, target, f"-DSPIRV-Headers_SOURCE_DIR={folders['spirv-headers']}", "-DSPIRV_SKIP_TESTS=ON", "-DSPIRV_WERROR=OFF")
    _cmake(folders["glslang"], build / "glslang", prefix, target, "-DENABLE_OPT=ON", "-DALLOW_EXTERNAL_SPIRV_TOOLS=ON", "-DGLSLANG_TESTS=OFF")
    _cmake(folders["spirv-cross"], build / "cross", prefix, target, "-DSPIRV_CROSS_SHARED=OFF", "-DSPIRV_CROSS_CLI=ON", "-DSPIRV_CROSS_ENABLE_TESTS=OFF")
    # libzstd.1.dylib 要随包（graphics/tools/shbake/zstd_lib.py 用 ctypes 加载），其余压缩库不链，Homebrew 版会带上 macOS 26 的 liblzma
    _cmake(folders["zstd"] / "build/cmake", build / "zstd", prefix, target, "-DZSTD_BUILD_SHARED=ON", "-DZSTD_BUILD_STATIC=OFF", "-DZSTD_PROGRAMS_LINK_SHARED=ON",
           "-DZSTD_BUILD_TESTS=OFF", "-DZSTD_LZMA_SUPPORT=OFF", "-DZSTD_LZ4_SUPPORT=OFF", "-DZSTD_ZLIB_SUPPORT=OFF")
    stamp.write_text(key)
    return prefix, folders
