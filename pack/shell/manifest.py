#!/usr/bin/env python3
"""壳的发版清单（Windows 全流程出包 #1091 / #1123，出包线 10-31）：我方按版本预编、与游戏无关的「壳」工具包，发版时出一份清单，
启动器下载后逐文件核 SHA-256、按壳编号与游戏包对号。

清单 = 产物契约 s2a.artifact.v2（pack/contracts/artifact.v2.schema.json），target = ios-shell，details 子格式 s2a.artifact.shell.v2：
  shellId     壳编号 = sha256("s2a.shell.v2\\0" + inputs + "\\0" + chain + "\\0" + iosMin + "\\0" + iosSdk) 前 16 位十六进制；游戏包写它要哪一版壳，
              出包和启动时与本地壳清单的 shellId 逐字相同才放行（不同就报红，换链号后头文件没跟上那类错配不再能装上）
  inputs      壳构建输入的指纹（维护者 11-04：壳编号 = 壳的构建输入，不再绑整份源码提交）= 输入表 pack/stage_inputs/shell.tsv 里「入」的文件 / 目录
              在 HEAD 上的对象号（同阶段键 pack/stage_keyfiles.sh stage_files_key）；表由出壳时实际读到的文件核（pack/shell/closure.py check，漏登记即红）。
              改文档等壳没读的文件壳编号不变，Windows 出包机按同一张表在本地算出壳编号、取对应的壳
  sourceCommit 构建这个壳的 40 位源码提交号（只作记录，不进壳编号）
  chain       直连库链号（translator/dmsh_chain.py version()：只跟宿主读库的接口版本走）
  iosMin      iOS 最低版本（tools/toolchain.env IPHONEOS_DEPLOYMENT_TARGET）
  iosSdk      壳链接的 iOS SDK 主版本（tools/toolchain.env IPHONEOS_SDK_MAJOR，审计 P1-124：换 SDK 要换编号，否则同号目录不覆盖、新系统机型按旧尺寸缩小）
files 列壳目录里每个普通文件的相对路径、大小、SHA-256（按路径排序）；壳目录不许有链接、特殊文件、仅大小写不同的两个路径（Windows 盘不分大小写）。

用法：
  manifest.py seal --root <壳目录> --manifest <目录外的清单> [--source-commit <40 位>] [--chain <链号>] [--ios-min <版本>]   缺省取本仓库当前提交 / 链号 / 部署目标
  manifest.py verify --root <壳目录> --manifest <清单>          逐文件重算：被改 / 缺失 / 多出的文件逐个点名，退出 1
  manifest.py require --manifest <清单> --shell-id <游戏包要的壳编号>   对号：不同就退出 1，两个编号都写出来
退出码：0 通过；1 校验 / 对号不过；2 用法或清单本身不合法
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pack.contracts.compare import digest_file
from pack.contracts.validate import ContractError, validate

SCHEMA = "s2a.artifact.shell.v2"
TARGET = "ios-shell"


class ShellError(ValueError):   # 用法或清单本身不合法（退出 2）
    pass


class Mismatch(ShellError):   # 壳目录与清单不符、壳编号对不上（退出 1）
    pass


def shell_id(inputs, chain, ios_min, ios_sdk):
    return hashlib.sha256(f"s2a.shell.v2\0{inputs}\0{chain}\0{ios_min}\0{ios_sdk}".encode()).hexdigest()[:16]


def inputs_key(repo=ROOT, rev="HEAD"):
    """输入表「入」的文件 / 目录在 rev 上的对象号指纹（= pack/stage_keyfiles.sh stage_files_key shell）；表里有不在 rev 的路径就报错"""
    rows = [l.rstrip("\n").split("\t") for l in (Path(repo) / "pack/stage_inputs/shell.tsv").read_text(encoding="utf-8").splitlines(True)]
    want = [r[1].rstrip("/") for r in rows if len(r) >= 2 and r[0] == "入"]
    if not want: raise ShellError("pack/stage_inputs/shell.tsv 里没有「入」")
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", rev, "--", *want], capture_output=True, check=True).stdout.decode("utf-8", "surrogateescape")
    lines = [l for l in out.splitlines() if l]
    got = {l.split("\t", 1)[1] for l in lines}
    miss = [w for w in want if w not in got]
    if miss: raise ShellError(f"壳输入表里有路径不在 {rev}：{' '.join(miss[:10])}（改名 / 删除要同笔改 pack/stage_inputs/shell.tsv）")
    body = "".join(f"{l.split()[2]} {l.split(chr(9), 1)[1]}\n" for l in lines)
    return hashlib.sha256(body.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _identity(source_commit, inputs, chain, ios_min, ios_sdk):
    if not isinstance(source_commit, str) or not re.fullmatch("[0-9a-f]{40}", source_commit):
        raise ShellError("构建提交号要 40 位十六进制")
    if not isinstance(inputs, str) or not re.fullmatch("[0-9a-f]{16}", inputs):
        raise ShellError("壳输入指纹要 16 位十六进制")
    if not isinstance(chain, str) or not re.fullmatch("[0-9a-z]{1,12}", chain):
        raise ShellError("直连链号不对（1–12 位）")
    if not isinstance(ios_min, str) or not re.fullmatch(r"\d+(\.\d+){0,2}", ios_min):
        raise ShellError("iOS 最低版本不对")
    if not isinstance(ios_sdk, str) or not re.fullmatch(r"\d{1,3}", ios_sdk):
        raise ShellError("iOS SDK 主版本不对")


def _root(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ShellError(f"壳目录不是普通目录：{root}")
    return root.resolve()


def _inventory(root):
    entries, folded = [], {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ShellError(f"壳目录里有链接或特殊文件：{rel}")
        if any(c in rel for c in "\\:\x00\t\r\n"):
            raise ShellError(f"路径含 Windows 不认的字符：{rel}")
        key = rel.casefold()
        if key in folded:
            raise ShellError(f"两个路径只差大小写（Windows 盘上会互相覆盖）：{folded[key]} / {rel}")
        folded[key] = rel
        if stat.S_ISREG(info.st_mode):
            sha = digest_file(path)
            if path.lstat().st_mtime_ns != info.st_mtime_ns or path.lstat().st_size != info.st_size:
                raise ShellError(f"读取期间文件变了：{rel}")
            entries.append({"path": rel, "size": info.st_size, "sha256": sha})
    if not entries:
        raise ShellError("壳目录是空的")
    return entries


def _document(root, source_commit, inputs, chain, ios_min, ios_sdk):
    _identity(source_commit, inputs, chain, ios_min, ios_sdk)
    doc = {"schema": "s2a.artifact.v2", "target": TARGET, "files": _inventory(root),
           "details": {"schema": SCHEMA, "shellId": shell_id(inputs, chain, ios_min, ios_sdk), "inputs": inputs,
                       "sourceCommit": source_commit, "chain": chain, "iosMin": ios_min, "iosSdk": ios_sdk}}
    validate("artifact", doc)
    return doc


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out: raise ShellError("清单里有重复字段")
        out[k] = v
    return out


def load(manifest):
    try:
        with open(manifest, encoding="utf-8") as f: doc = json.load(f, object_pairs_hook=_pairs)
        validate("artifact", doc)
    except (OSError, ValueError, ContractError) as e:
        raise ShellError(f"清单读不了或不合契约：{e}") from e
    if set(doc) != {"schema", "target", "files", "details"} or doc["target"] != TARGET:
        raise ShellError("不是壳清单（target 要 ios-shell）")
    d = doc["details"]
    if set(d) != {"schema", "shellId", "inputs", "sourceCommit", "chain", "iosMin", "iosSdk"} or d["schema"] != SCHEMA:
        raise ShellError(f"未知壳清单子格式（要 {SCHEMA}）")
    _identity(d["sourceCommit"], d["inputs"], d["chain"], d["iosMin"], d["iosSdk"])
    if d["shellId"] != shell_id(d["inputs"], d["chain"], d["iosMin"], d["iosSdk"]):
        raise ShellError("清单里的壳编号与它记的输入指纹 / 链号 / iOS 版本对不上")
    for e in doc["files"]:
        if set(e) != {"path", "size", "sha256"}: raise ShellError("文件条目字段不对")
    return doc


def verify(root, manifest):   # → 清单；不符时 ShellError，消息逐个点名被改 / 缺失 / 多出的文件
    root = _root(root); doc = load(manifest)
    want = {e["path"]: (e["size"], e["sha256"]) for e in doc["files"]}
    have = {e["path"]: (e["size"], e["sha256"]) for e in _inventory(root)}
    bad = [f"被改：{p}" for p in sorted(want.keys() & have.keys()) if want[p] != have[p]]
    bad += [f"缺失：{p}" for p in sorted(want.keys() - have.keys())] + [f"多出：{p}" for p in sorted(have.keys() - want.keys())]
    if bad:
        raise Mismatch(f"壳 {doc['details']['shellId']} 校验不过（{len(bad)} 处）：" + "；".join(bad[:20]) + (" …" if len(bad) > 20 else ""))
    return doc


def validate_download_paths(entries):
    """Validate all file and parent paths before a downloader creates anything."""
    seen = {}
    for entry in entries:
        parts = entry["path"].split("/")
        for index, part in enumerate(parts):
            stem = part.split(".", 1)[0].upper()
            if (not part or part in (".", "..") or part.endswith((" ", "."))
                    or any(ord(c) < 32 or c in '\\:<>"|?*' for c in part)
                    or stem in {"CON", "PRN", "AUX", "NUL"}
                    or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem)):
                raise ShellError(f"下载路径不能在 Windows 安全落盘：{entry['path']}")
            path = "/".join(parts[:index + 1])
            kind = "file" if index == len(parts) - 1 else "dir"
            key = path.casefold()
            if key in seen and (seen[key] != (path, kind) or kind == "file"):
                raise ShellError(f"下载路径大小写冲突、重复或文件目录重叠：{path}")
            seen[key] = (path, kind)


def require(manifest, need):
    doc = load(manifest); have = doc["details"]["shellId"]
    if have != need:
        raise Mismatch(f"游戏包要壳 {need}，本地壳是 {have}（提交 {doc['details']['sourceCommit'][:10]}、链号 {doc['details']['chain']}）：取对应版本的壳")
    return doc


def seal(root, manifest, source_commit, inputs, chain, ios_min, ios_sdk):
    root = _root(root); dest = Path(manifest)
    if dest.is_symlink() or dest.resolve().is_relative_to(root):
        raise ShellError("清单要写在壳目录外面，且不能是链接")
    if dest.exists():
        raise ShellError(f"不覆盖已有清单：{dest}")
    doc = _document(root, source_commit, inputs, chain, ios_min, ios_sdk)
    data = (json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, tmp = tempfile.mkstemp(prefix=".shell-", dir=dest.resolve().parent)
    try:
        with os.fdopen(fd, "wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
        verify(root, tmp)   # 发布前按清单把壳目录再核一遍
        os.link(tmp, dest)   # 不覆盖地原子发布
    except FileExistsError as e:
        raise ShellError(f"不覆盖已有清单：{dest}") from e
    finally:
        Path(tmp).unlink(missing_ok=True)
    return doc


def _defaults():   # 本仓库当前提交、壳输入指纹、直连链号、iOS 部署目标
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    sys.path.insert(0, str(ROOT / "translator")); import dmsh_chain
    env = (ROOT / "tools/toolchain.env").read_text()
    m = re.search(r"^IPHONEOS_DEPLOYMENT_TARGET\s*[:?]?=\s*(\S+)", env, re.M)
    sdk = re.search(r"^IPHONEOS_SDK_MAJOR\s*[:?]?=\s*(\S+)", env, re.M)
    return commit, inputs_key(), dmsh_chain.version(), m.group(1) if m else "", sdk.group(1) if sdk else ""


def current_id():   # 本树该用的壳编号：出包取壳、组装核壳都用它
    """开发树：按输入表在 HEAD 上算。发布快照（App / 启动器内置引擎，根有 engine.json）：读打包时写进去的 shellId——
    快照只收部分目录、还剔了测试目录，在快照里按目录对象号重算会与出壳时不同（platforms/bundle_engine.py write_manifest 写）"""
    eng = ROOT / "engine.json"
    if eng.is_file():
        sid = json.loads(eng.read_text(encoding="utf-8")).get("shellId")
        if not (isinstance(sid, str) and re.fullmatch("[0-9a-f]{16}", sid)):
            raise ShellError("发布快照的 engine.json 里没有壳编号 shellId（引擎打包时没写，重新打包引擎）")
        return sid
    _, inputs, chain, ios_min, ios_sdk = _defaults()
    return shell_id(inputs, chain, ios_min, ios_sdk)


def main():
    ap = argparse.ArgumentParser(description="壳的发版清单：seal / verify / require")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("seal"); p.add_argument("--root", required=True); p.add_argument("--manifest", required=True)
    p.add_argument("--source-commit"); p.add_argument("--inputs"); p.add_argument("--chain"); p.add_argument("--ios-min"); p.add_argument("--ios-sdk")
    p = sub.add_parser("verify"); p.add_argument("--root", required=True); p.add_argument("--manifest", required=True)
    p = sub.add_parser("require"); p.add_argument("--manifest", required=True); p.add_argument("--shell-id", required=True)
    a = ap.parse_args()
    try:
        if a.cmd == "seal":
            c, ik, ch, im, sdk = (a.source_commit, a.inputs, a.chain, a.ios_min, a.ios_sdk) if a.source_commit and a.inputs and a.chain and a.ios_min and a.ios_sdk else _defaults()
            doc = seal(a.root, a.manifest, a.source_commit or c, a.inputs or ik, a.chain or ch, a.ios_min or im, a.ios_sdk or sdk)
            print(f"🟢 壳清单 {doc['details']['shellId']}：{len(doc['files'])} 个文件 → {a.manifest}")
        elif a.cmd == "verify":
            doc = verify(a.root, a.manifest); print(f"🟢 壳 {doc['details']['shellId']} 校验通过：{len(doc['files'])} 个文件")
        else:
            doc = require(a.manifest, a.shell_id); print(f"🟢 壳编号对上：{doc['details']['shellId']}")
        return 0
    except ShellError as e:
        print(f"【红】{e}", file=sys.stderr)
        return 1 if isinstance(e, Mismatch) else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
