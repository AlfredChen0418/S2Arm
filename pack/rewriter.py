#!/usr/bin/env python3
"""Resolve the packaging rewriter; build the host C CLI offline when needed."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def backend():
    # 10-31 维护者 拍板删 Python 改写器（patch_nso.py）：只剩 C 改写器，产物正确性靠金样字节 + 进局验证，不再维护第二份实现
    value = os.environ.get("S2A_REWRITER", "c")
    if value != "c":
        raise ValueError("Python 改写器已删除（S2A_REWRITER 只认 c）")
    return value


def override():
    value = os.environ.get("S2A_REWRITER_C")
    if not value:
        return None
    path = Path(value).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("S2A_REWRITER_C 指定的 C 命令行不存在或不可执行")
    return path


def source_digest():
    # Contents, rather than Git HEAD or mtimes, also cover an uncommitted edit.
    digest = hashlib.sha256()
    for name in ("CMakeLists.txt", "main.c", "rewriter.c", "rewriter.h", "prepare_deps.py"):
        digest.update(name.encode() + b"\0" + (ROOT / "runtime/rewriter" / name).read_bytes())
    return digest.hexdigest()


def app_runtime():
    """Recognize only the Python shipped in the fixed S2Arm.app Runtime layout."""
    exe = Path(sys.executable).absolute()
    bindir = exe.parent
    python = bindir.parent
    runtime = python.parent
    resources = runtime.parent
    contents = resources.parent
    app = contents.parent
    if (bindir.name != 'bin' or python.name != 'python' or runtime.name != 'Runtime'
            or resources.name != 'Resources' or contents.name != 'Contents'
            or app.suffix != '.app' or not exe.name.startswith('python3')):
        return None
    return runtime


def prebuilt_path():
    # Windows 启动器随发版预编的一份（launchers/windows/bundle_prepare_win.py → S2A_PREBUILT_TOOLS/rewriter/，旁边同样写 .source）
    tools = os.environ.get("S2A_PREBUILT_TOOLS")
    return str(Path(tools) / "rewriter" / ("s2a-rewrite.exe" if os.name == "nt" else "s2a-rewrite")) if tools else None


def bundled():
    # App 自带的命令行（launchers/macos/bundle_runtime.py 编进 Runtime/bin，旁边 .source 记编它的源码指纹）：
    # 只在指纹等于本引擎源码时用；引擎比 App 新时不用旧二进制
    runtime = app_runtime()
    value = str(runtime / 'bin/s2a-rewrite') if runtime else (os.environ.get("S2A_REWRITER_BUNDLED") or prebuilt_path())
    if not value:
        return None
    path = Path(value).resolve()
    stamp = path.with_name(path.name + ".source")
    if (path.is_file() and os.access(path, os.X_OK) and stamp.is_file()
            and stamp.read_text().strip() == source_digest()):
        return path
    return None


def key():
    mode = backend()
    path = override() or bundled()
    return mode + (":" + hashlib.sha256(path.read_bytes()).hexdigest() if path else "")


def dependency_dir():
    candidates = []
    if os.environ.get("S2A_REWRITER_DEPS"):
        candidates.append(Path(os.environ["S2A_REWRITER_DEPS"]))
    candidates.extend((ROOT / "build/rewriter-deps",
                       Path(os.environ.get("S2A_RUN_ROOT") or Path.home() / ".s2arm/run") / "s2arm/deps/rewriter-deps"))
    for candidate in candidates:
        if ((candidate / "capstone-5.0.7/src/CMakeLists.txt").is_file()
                and (candidate / "lz4-4.4.5/lz4libs/lz4.c").is_file()):
            return candidate.resolve()
    # 报实际查过的路径（11-07 联合开发者反馈：原文案把「${S2A_RUN_ROOT:-$HOME/.s2arm/run}/…」字面量当回退路径列出，像是要他照我方开发机布置）
    raise ValueError("缺 C 改写器固定依赖（钉版 Capstone 5.0.7 / LZ4 4.4.5 源码）：查过 " + "、".join(map(str, candidates)) +
                     "，都不完整。从源码构建先跑一键准备 python3 launchers/macos/prepare_build.py（下载到本树 build/rewriter-deps），"
                     "或 python3 runtime/rewriter/prepare_deps.py <目录> 后设 S2A_REWRITER_DEPS=<目录>；构建本身不会自动下载。")


def c_path():
    path = override() or bundled()
    if path:
        return path
    if app_runtime():
        raise ValueError("App 自带 C 改写器缺失或源码指纹不符；请更新或修复 S2Arm.app。")
    stale = ("App 自带 C 改写器与本引擎源码指纹不符（引擎比 App 新），不用旧二进制；"
             if os.environ.get("S2A_REWRITER_BUNDLED") else "")
    source = ROOT / "runtime/rewriter"
    digest = hashlib.sha256(source_digest().encode() + b"\0")
    for value in (platform.system(), platform.machine(), *(os.environ.get(k, "") for k in
                   ("CC", "CFLAGS", "CPPFLAGS", "LDFLAGS", "CMAKE_TOOLCHAIN_FILE", "MACOSX_DEPLOYMENT_TARGET"))):
        digest.update(value.encode() + b"\0")
    if os.name == "nt": digest.update(b"msvcrt=static\0")   # Windows 静态 C 运行库（下面 cmake 参数）进缓存键：旧的动态运行库那份不复用
    cache = Path(os.environ.get("S2A_REWRITER_CACHE", ROOT / "build/rewriter-cli")).resolve()
    name = "s2a-rewrite.exe" if os.name == "nt" else "s2a-rewrite"
    binary = cache / digest.hexdigest() / name
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    try:
        deps = dependency_dir()
    except ValueError as error:
        raise ValueError(stale + str(error))
    if not shutil.which("cmake"):
        raise ValueError(stale + "构建 C 改写器需要 CMake 3.18+ 和 C 编译器；"
                         "也可用 S2A_REWRITER_C 指定已编译的命令行")
    cache.mkdir(parents=True, exist_ok=True)
    # Each builder owns its temporary tree on the cache's filesystem. Rename
    # publishes a complete executable, including on filesystems without links.
    with tempfile.TemporaryDirectory(prefix=".build-", dir=cache) as tmp:
        subprocess.run(["cmake", "-S", str(source), "-B", tmp,
                        "-DS2A_REWRITER_DEPS=" + str(deps), "-DCMAKE_BUILD_TYPE=Release"]
                       + (["-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded", "-DCMAKE_POLICY_DEFAULT_CMP0091=NEW"] if os.name == "nt" else []),   # Windows 静态 C 运行库：玩家机不用另装 VC++ 运行库
                       check=True, stdout=sys.stderr)
        subprocess.run(["cmake", "--build", tmp, "--config", "Release", "--target", "s2a-rewrite", "--parallel", "2"],
                       check=True, stdout=sys.stderr)
        built = Path(tmp) / name
        if not built.is_file():
            built = Path(tmp) / "Release" / name
        if not built.is_file() or not os.access(built, os.X_OK):
            raise ValueError("C 改写器构建没有生成可执行命令行")
        binary.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(built, binary)
        except OSError:
            # Windows may refuse replacement when another caller is already
            # running the complete executable published by a concurrent build.
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise
    return binary


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["key"]:
            print(key())
        elif sys.argv[1:] == ["c-path"] and backend() == "c":
            print(c_path())
        elif sys.argv[1:] == ["source"]:
            print(source_digest())   # C 改写器的编译输入指纹（改写阶段键用：说明文档等不进键）
        elif sys.argv[1:] == ["deps-path"]:
            print(dependency_dir())   # 门禁另编 C 接口测试（tests/rewrite_c_gate_test.sh）与出包用同一份固定依赖
        else:
            raise ValueError("用法：rewriter.py key | c-path | source | deps-path")
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("【红】改写器：" + str(error), file=sys.stderr)
        sys.exit(1)
