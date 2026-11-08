#!/usr/bin/env python3
"""Build-stage selector. JSON is staging metadata, never a runtime protocol.
No build/run. --key plans without writes; normal mode prepares provisional data.
"""
import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "graphics/nvn/tools"))
import direct_profile as dp


def options(root, tid, version, display, name):
    metadata = {}
    if (root / "unpack.tsv").is_file():
        for line in (root / "unpack.tsv").read_text().splitlines():
            c = line.split("\t")
            if len(c) == 2:
                dp.require(c[0] not in metadata, "duplicate unpack.tsv metadata key")
                metadata[c[0]] = c[1]
    if "title_id" in metadata:
        dp.require(metadata["title_id"].upper() == tid, "unpack.tsv identity differs from NPDM")
    sidecar = root / "direct-profile-input.json"
    if sidecar.exists():
        data = json.loads(sidecar.read_text())
        dp.require(isinstance(data, dict), "profile input metadata must be an object")
        dp.require(all(k not in data or isinstance(data[k], str) for k in ("title_id", "display", "name")),
                   "profile input metadata strings invalid")
        dp.require(data.get("format") == 1 and str(data.get("title_id", "")).upper() == tid,
                   "profile input metadata identity/format mismatch")
        cv = data.get("content_version")
        dp.require(type(cv) is int and 0 <= cv <= 0xffffffff, "invalid inspected content version")
        dp.require(version is None or version == f"v{cv}", "version differs from inspected content")
        version = f"v{cv}"
        display = display or data.get("display")
        name = name or data.get("name")
    raw = metadata.get("update", "")
    if raw:
        dp.require(raw.isdecimal(), "invalid update content version")
        dp.require(version is None or version == f"v{raw}", "version differs from unpack.tsv update")
        version = f"v{raw}"
    if version is None and metadata.get("base", "").isdecimal():   # 没有更新取本体版本（同 direct_profile_input.content_version）。#36：Mac 打包不写旁注，只有本体的游戏走到这里被拒，
        version = f"v{metadata['base']}"                            #   修在消费端，已种好的产物根重试也能过
    dp.require(version is not None, "missing content version: pass --version v<integer>")
    nacp_name, nacp_display = nacp_identity(root)
    return {"version": version, "display": display or metadata.get("display_version") or nacp_display or "unknown",
            "name": name or metadata.get("name") or nacp_name or tid}


def nacp_identity(root):
    """名字 / 显示版本兜底：产物根没有 unpack.tsv 时（播种的根只带 ctl/control.nacp）读游戏自带的控制数据。
    名字取第一个非空语言项（项 0 = 美式英语，每项 0x300 字节、名字占前 0x200），显示版本在 +0x3060（0x10 字节）；读不出返回 (None, None)"""
    p = root / "ctl/control.nacp"
    if not p.is_file():
        return None, None
    d = p.read_bytes()
    if len(d) != 0x4000:
        return None, None
    text = lambda b: b.split(b"\0")[0].decode("utf-8", "replace").strip() or None
    name = next((n for n in (text(d[i * 0x300:i * 0x300 + 0x200]) for i in range(16)) if n), None)
    return name, text(d[0x3060:0x3070])


def plan(root, *, version=None, display=None, name=None):
    root = Path(root).resolve()
    tid = dp.validate_npdm(root / "exefs/main.npdm")
    selected = dp.game.find_dir(tid)
    result = {"format": 1, "title_id": tid, "runtime_verified": False}
    has_profile = (selected / "game.tsv").is_file()
    generated = has_profile and ((selected / "survey.json").exists() or
                 (selected / "game.tsv").read_text().startswith("# Provisional game profile"))
    registered = has_profile and selected.resolve() == (dp.NX / "games" / tid).resolve()
    if registered or has_profile and not generated:
        dp.validate_profile(selected, tid)
        result.update(profile=str(selected.resolve()), provisional=False,
                      source="registered" if registered else "external-manual",
                      build_profile_ready=True)
        inputs = {"profile": [(p.name, dp.file_digest(p)) for p in sorted(selected.iterdir()) if p.is_file()]}
    else:
        opts = options(root, tid, version, display, name)
        result.update(provisional=True, source="generated", options=opts)
        if generated:
            inputs = dp.provenance(root, None); inputs["metadata"] = {"version": opts["version"], "display": opts["display"], "name": opts["name"] or tid}
            dp.verify_generated(selected, tid, dp.input_fingerprint(root, tid, **opts), opts["version"])
            result["existing_profile"] = str(selected.resolve())
            inputs["profile_files"] = [(p.name, dp.file_digest(p)) for p in sorted(selected.iterdir()) if p.is_file()]
        else:
            inputs = dp.provenance(root, None)
            inputs["metadata"] = opts
    inputs["selector"] = dp.file_digest(Path(__file__))
    inputs["stage"] = dp.file_digest(Path(__file__).with_name("stage_direct_profile.sh"))
    result["key"] = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    return result


def select(root, out, *, version=None, display=None, name=None):
    result = plan(root, version=version, display=display, name=name)
    if result["source"] == "generated":
        opts = result.pop("options")
        existing = result.pop("existing_profile", None)
        if existing:
            # Snapshot a validated automatic profile, preserving its manual edits.
            # --key never calls this branch and never writes.
            target = dp.validate_output(root, out) / result["title_id"]
            if target != Path(existing):
                if target.exists():
                    dp.prepare(root, target.parent, **opts)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix=".direct-profile-", dir=target.parent) as tmp:
                        copy = Path(tmp) / target.name
                        dp.require(not any(p.is_symlink() for p in Path(existing).rglob("*")),
                                   "generated profile symlinks unsupported")
                        shutil.copytree(existing, copy)
                        os.rename(copy, target)
            result.update(profile=str(target), reused=True)
        else:
            result.update(dp.prepare(root, out, **opts))
        result["DM_GAMES_DIR"] = str(Path(result["profile"]).parent)
    return result


REPORT_FILES = {"survey.json", "survey.md"}   # 推导报告：给人看与溯源，出库不读，不进 profile_key


def profile_key(directory):
    """推导档内容键（直连阶段键的一部分）：只算出库会读的档文件。survey.json / survey.md 是推导报告（含推导脚本自身的哈希与输入清单），
    出库不读；算进来则推导脚本改一行、推导结果逐字节没变的款也全部换键重出直连库（11-02 维护者：键要挂在产物上，不挂在脚本上）"""
    digest = [(p.name, dp.file_digest(p)) for p in sorted(Path(directory).iterdir()) if p.is_file() and p.name not in REPORT_FILES]
    return hashlib.sha256(json.dumps(digest).encode()).hexdigest()


def selected(root):
    """Read-only build selection after the versioned configuration phase."""
    root = Path(root).resolve()
    result = plan(root)
    if result["source"] == "generated":
        record = root / "direct_profile.json"
        dp.require(record.is_file(), "configuration phase has not prepared a profile")
        data = json.loads(record.read_text())
        dp.require(isinstance(data, dict) and data.get("format") == 1 and
                   data.get("title_id") == result["title_id"], "invalid profile selection metadata")
        relative = Path(data.get("profile", ""))
        dp.require(not relative.is_absolute() and relative.parts and
                   relative.parts[0] == "direct-profile" and ".." not in relative.parts,
                   "profile selection path must stay in artifact direct-profile")
        directory = (root / relative).resolve()
        dp.require(root in directory.parents, "profile selection escapes artifact root")
        opts = result.pop("options")
        dp.verify_generated(directory, result["title_id"], dp.input_fingerprint(root, result["title_id"], **opts), opts["version"])
    else:
        directory = Path(result["profile"])
    return {"format": 1, "title_id": result["title_id"], "source": result["source"],
            "provisional": result["provisional"], "runtime_verified": False,
            "profile": str(directory), "build_profile_ready": True,
            "profile_key": profile_key(directory)}



def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--key", action="store_true")
    p.add_argument("--selected", action="store_true")
    p.add_argument("--version")
    p.add_argument("--display")
    p.add_argument("--name")
    p.add_argument("--walk", type=Path, help="RomFS 清单目录（缺省 <产物根上一级>/.romfs-walk；按 RomFS 内容身份命名，跨进程跨阶段共用读头结果，不进键；"
                   "接口见 graphics/nvn/tools/README.profile.md「RomFS 清单」）")
    a = p.parse_args()
    try:
        kwargs = {"version": a.version, "display": a.display, "name": a.name}
        # plan 的 provenance 与 inspect_inputs 的三项检查共用一次 RomFS 遍历（direct_profile.romfs_walk）；读头结果按内容身份存进清单目录，
        #   本阶段其余进程与抽取 / 直连阶段里的 --selected 直接读（README.profile.md「RomFS 清单」）
        env = os.environ.get("S2A_ROMFS_WALK")   # 同 romfs_list.default_listing：给了就用（off = 不落盘）
        if a.walk: walk = a.walk
        elif env: walk = None if env == "off" else Path(env)
        else: walk = a.root.resolve().parent / ".romfs-walk"
        with dp.one_walk(walk):
            if a.selected:
                result = selected(a.root)
            elif a.key:
                result = plan(a.root, **kwargs)
            else:
                dp.require(a.out is not None, "--out required when preparing")
                result = select(a.root, a.out, **kwargs)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except dp.Unsupported as e:   # 内容缺口：本款用了直连还没实现的结构 → 退 3，refresh 打黄走通用（10-25 维护者）
        print(f"【黄】直连缺口：配置推导不支持本款：{e}", file=sys.stderr)
        return 3
    except (ValueError, OSError, KeyError, RuntimeError) as e:
        print(f"【红】直连配置阶段拒绝：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
