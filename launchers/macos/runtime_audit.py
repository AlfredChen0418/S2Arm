"""Reject runtime links to a release builder's Homebrew or checkout."""
from pathlib import Path
import re
import subprocess

SYSTEM_PATHS=('/usr/lib/','/System/Library/')


def macho(path):
    with path.open('rb') as stream:
        return stream.read(4) in (b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe',b'\xca\xfe\xba\xbf')


def rpaths(commands):
    return re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset',commands)


def check_load_paths(path,dependencies,commands):
    for line in dependencies.splitlines():
        # Universal binaries repeat a filename/architecture header for each slice.
        if ' (compatibility version' not in line:
            continue
        value=line.strip().split(' (compatibility')[0]
        if value.startswith('/') and not value.startswith(SYSTEM_PATHS):
            raise RuntimeError(f'External runtime dependency in {path}: {value}')
    for value in rpaths(commands):
        if value.startswith('/') and not value.startswith(SYSTEM_PATHS):
            raise RuntimeError(f'External runtime search path in {path}: {value}')


def minimum_macos(build_info):
    """vtool -show-build 的输出 → 所有 macOS 分片的最低系统（iOS 分片不算）。"""
    found=[];platform=None
    for line in build_info.splitlines():
        words=line.split()
        if len(words)>=2 and words[0]=='platform': platform=words[1]
        elif len(words)>=2 and words[0]=='minos' and platform=='MACOS': found.append(words[1])
    return found


def version_tuple(text):
    return tuple(int(part) for part in text.split('.'))


def verify_minimum_macos(app,declared):
    """App 声明支持 declared，包内每个 macOS 程序的最低系统都不能更高。返回检查的 Mach-O 个数。"""
    app=Path(app).resolve();limit=version_tuple(declared);bad=[];count=0
    for path in app.rglob('*'):
        if path.is_symlink() or not path.is_file() or not macho(path):
            continue
        count+=1
        for minimum in minimum_macos(subprocess.check_output(['vtool','-show-build',path],text=True)):
            if version_tuple(minimum)>limit:
                bad.append(f'{path.relative_to(app)} (macOS {minimum})')
                break
    if bad:
        raise RuntimeError(f'【红】{len(bad)} 个程序要求的 macOS 高于声明的 {declared}：\n  '+'\n  '.join(bad))
    return count


def verify_portable_runtime(runtime):
    runtime=Path(runtime).resolve();count=0
    for path in runtime.rglob('*'):
        if path.is_symlink():
            if path.readlink().is_absolute() or not path.resolve().is_relative_to(runtime):
                raise RuntimeError(f'External runtime symlink: {path}')
        elif path.is_file() and macho(path):
            dependencies=subprocess.check_output(['otool','-L',path],text=True)
            commands=subprocess.check_output(['otool','-l',path],text=True)
            check_load_paths(path,dependencies,commands);count+=1
    return count
