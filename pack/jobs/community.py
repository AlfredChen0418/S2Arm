"""社区资源（10-30 维护者 拍板：社区补丁一律学成我方通用能力，公开发布的包不带上游补丁字节）：宽屏 / 60 帧是我方能力，在宿主层运行时开关（缺省开、可关，下次启动生效）；
画面类社区补丁（other-gfx）只在内部开发 / 测试包里列出（S2A_COMMUNITY_PATCHES=1，同 pack/stage_rewrite.sh 打补丁字节的开关），玩家包缺省不列、不打。
面板显示这款有没有宽屏表 / 60 帧表、来自哪（已验证名单 / 社区包；版本对不上的由直连线打包时跨版本定位，不判不可用），
画面类（other-gfx）逐项开关，着色器缓存命中就自动并入（只显示并入数，导入入口留给玩家导自己的缓存）。

数据来源（只读，读不到就当没有，不报错）：
  宽屏已验证：引擎 graphics/widen/tables/verified.tsv（程序号 + main 构建号）
  社区包：S2A_COMMUNITY_DIR（没设 = 没有社区包；11-07 起不再缺省读开发机数据盘，启动器进程也不读 tools/machine.local）下 */full/*/INDEX.tsv（第一轮）与 */*/INDEX.tsv（第二轮，按类分目录），-lite 子集包不收——有 aspect 列的是宽屏包，有 target_fps 列的是帧率包
  画面：补丁应用层（直连线）出的 graphics/community/other_gfx.tsv：程序号 版本 编号 名称 互斥组 已验证(0/1) 来源
玩家的选择存在游戏库 games/<程序号>/community.json：{"schema":2,"widescreen":真/假,"fps60":真/假,"enabled":{画面编号:真/假}}。
  宽屏 / 60 帧只是宿主设置：Mac 启动时由 build.launch 转成 S2A_WIDESCREEN=0 / S2A_FPS60=0；iPhone 在 App 设置页（Settings.bundle）开关
  S2AWidescreen / S2AFps60（ios/settings.m 映射成同名环境变量）。
Mac 设置页多两项：widenForce（同 iPhone「宽屏强制启用」）、metalfx（只 Mac，缺省关）；schema 3 起只存玩家明确选过的，没选的跟能力清单走。"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from core import Failure, atomic, contained


def _rows(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader((l for l in f if not l.startswith("#")), delimiter="\t"))
    except (OSError, UnicodeError, csv.Error):
        return []


def game_of(c, gid):
    try:
        catalog = json.loads((c.library / "catalog.json").read_text())
    except (OSError, ValueError):
        catalog = {}
    game = next((g for g in catalog.get("games", []) if g.get("id") == gid), None)
    if not game:
        raise Failure("游戏库里没有这款游戏。", code="input_missing")
    return game


def _version(game):
    """本机游戏实际跑的版本：有更新取最新更新的显示版本，否则本体"""
    packs = sorted(game.get("packages", []), key=lambda p: (p.get("type") == "update", p.get("version", 0)))
    shown = [p.get("display") for p in packs if p.get("type") in ("base", "update") and p.get("display")]
    return shown[-1] if shown else ""


def _norm(v):
    return (v or "").strip().lower().lstrip("v")


def _community_dir():
    d = os.environ.get("S2A_COMMUNITY_DIR")
    return Path(d) if d else None


def _indexes():
    """社区包索引（同 pack/stage_rewrite.sh 的 S2A_CPATCH_DIRS 缺省）：第一轮 <包>/full/<名>/INDEX.tsv、第二轮 <包>/<类>/INDEX.tsv；名字以 -lite 结尾的包是同轮子集，不收"""
    d = _community_dir()
    if d is None:
        return []
    return [p for p in sorted([*d.glob("*/full/*/INDEX.tsv"), *d.glob("*/*/INDEX.tsv")]) if not p.relative_to(d).parts[0].endswith("-lite")]


def _saved(c, gid):
    try:
        data = json.loads(contained(c.library, c.library / "games" / gid / "community.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def runtime_caps(c, gid):
    """这款 Mac 运行版本的能力清单 s2a_caps.tsv（出包时生成）：{能力: 值}。没有清单 = 这款还没有 Mac 运行版本或版本太旧"""
    try:
        text = contained(c.library, c.library / "games" / gid / "current" / "s2a_caps.tsv").read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError):
        return {}
    rows = (line.split("#", 1)[0].split("\t") for line in text.splitlines() if not line.startswith("#"))
    return {r[0].strip(): r[1].strip() for r in rows if len(r) >= 2 and r[0].strip()}


def manual_fps60(saved):
    """玩家在设置页明确开过 60 帧。schema 2 时存盘总带 fps60=真（旧界面改别的开关也会写），不算玩家选的"""
    return saved.get("schema") == 3 and saved.get("fps60") is True


NO_RUNTIME = "这款还没有做好 Mac 版，或者是旧版本做的：重新准备后可用。"
NO_FPS60 = "这款游戏没有可用的 60 帧方案。"
NO_DIRECT = "这款游戏没有走直连渲染，用不了 MetalFX 放大。"


def mac_settings(c, gid):
    """Mac 设置页的三个开关：每项 {available, enabled, reason}。缺省跟能力清单走（60 帧一般开，满屏强制和 MetalFX 关）"""
    caps, saved = runtime_caps(c, gid), _saved(c, gid)
    def row(available, enabled, reason):
        return dict(available=available, enabled=available and enabled, reason="" if available else (NO_RUNTIME if not caps else reason))
    fps = saved.get("fps60") is not False and (manual_fps60(saved) or caps.get("fps60") == "1")
    return dict(fps60=row("fps60" in caps, fps, NO_FPS60),
                widenForce=row(bool(caps), saved.get("widenForce") is True, ""),
                metalfx=row(caps.get("direct") == "1", saved.get("metalfx") is True, NO_DIRECT))


def selection(c, gid):
    """玩家选择；读不到 / 旧格式按缺省（宽屏、60 帧开）"""
    try:
        data = json.loads(contained(c.library, c.library / "games" / gid / "community.json").read_text())
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return dict(widescreen=data.get("widescreen", True) is not False, fps60=data.get("fps60", True) is not False,
                enabled={str(k): bool(v) for k, v in (data.get("enabled") or {}).items()} if isinstance(data.get("enabled"), dict) else {})


def available(c, game):
    gid, version = game["id"].upper(), _version(game)
    builds = {(p.get("builds") or {}).get("main", "").lower() for p in game.get("packages", [])} - {""}
    ws = dict(verified=False, sources=[], relocate=False)
    for r in _rows(c.engine / "graphics/widen/tables/verified.tsv"):
        if (r.get("程序号") or "").upper() == gid and (r.get("构建号") or "").lower() in builds:
            ws["verified"] = True
    fps = dict(verified=False, sources=[], relocate=False)
    for index in _indexes():
        rows = _rows(index)
        kind = ws if rows and "aspect" in rows[0] else fps if rows and "target_fps" in rows[0] else None
        for r in rows if kind is not None else []:
            if (r.get("title_id") or "").upper() != gid or not (r.get("in_pack") or "").startswith("yes"):
                continue
            if kind is fps and (r.get("target_fps") or "") != "60":
                continue
            if r.get("source") and r["source"] not in kind["sources"]:
                kind["sources"].append(r["source"])
            if r.get("version") and _norm(r["version"]) != _norm(version):
                kind["relocate"] = True   # 版本对不上：打包时由直连线按特征跨版本定位（定不到就不打，不影响出包）
    gfx = []
    for r in _rows(c.engine / "graphics/community/other_gfx.tsv") if os.environ.get("S2A_COMMUNITY_PATCHES") == "1" else []:   # 画面补丁只能靠补丁字节生效：玩家包不带字节，就不列
        if (r.get("程序号") or "").upper() != gid:
            continue
        gfx.append(dict(id="gfx:" + (r.get("编号") or ""), label=r.get("名称") or r.get("编号") or "",
                        exclusive=r.get("互斥组") or "", source=r.get("来源") or "",
                        state="verified" if r.get("已验证") == "1" else "community",
                        relocate=bool(r.get("版本")) and _norm(r.get("版本")) != _norm(version)))
    return dict(widescreen=ws, fps60=fps, graphics=gfx)


def describe_game(c, game):
    a, s = available(c, game), selection(c, game["id"])
    for key in ("widescreen", "fps60"):
        a[key]["available"] = a[key]["verified"] or bool(a[key]["sources"])
        a[key]["enabled"] = a[key]["available"] and s[key]
    for it in a["graphics"]:
        it["enabled"] = bool(s["enabled"].get(it["id"], it["state"] == "verified"))
    return dict(gameId=game["id"], version=_version(game), mac=mac_settings(c, game["id"]), **a)


def listing(c):
    game = game_of(c, c.r.get("gameId") or "")
    c.begin("读取社区资源")
    return describe_game(c, game)


def save(c):
    """请求可带 widescreen / fps60（真假）与 graphics {画面编号: 真/假}；画面同互斥组至多开一个；只认本款列得出的编号"""
    game = game_of(c, c.r.get("gameId") or "")
    a, s = available(c, game), selection(c, game["id"])
    for key in ("widescreen", "fps60"):
        if key in c.r:
            s[key] = bool(c.r[key])
    want = c.r.get("graphics") or {}
    if not isinstance(want, dict):
        raise Failure("社区资源选择格式不对。", code="job_failed")
    every = {it["id"]: it for it in a["graphics"]}
    for key, on in want.items():
        it = every.get(key)
        if it is None:
            raise Failure("这款游戏没有这个补丁。", code="job_failed")
        s["enabled"][key] = bool(on)
        if on and it["exclusive"]:
            for other in every.values():
                if other["exclusive"] == it["exclusive"] and other["id"] != key:
                    s["enabled"][other["id"]] = False
    saved = _saved(c, game["id"])
    out = dict(schema=3, enabled=s["enabled"])
    for key in ("widescreen", "fps60", "widenForce", "metalfx"):   # 只存玩家明确选过的；没选的继续跟能力清单走
        if key in c.r:
            out[key] = bool(c.r[key])
        elif key in saved and not (key == "fps60" and saved[key] is True and saved.get("schema") != 3):
            out[key] = saved[key]
    atomic(contained(c.library, c.library / "games" / game["id"] / "community.json"), out)
    return describe_game(c, game)


def launch_env(c, gid):
    """Mac 启动时的宿主开关（关掉的才设；与 iPhone 设置页同口径）"""
    s, env, saved, caps = selection(c, gid), {}, _saved(c, gid), runtime_caps(c, gid)
    if manual_fps60(saved) and caps.get("fps60") == "0":
        env["S2A_FPS60"] = "1"            # 清单因强信号关了 60 帧，玩家手动打开（同 iPhone 设置页）
    if saved.get("widenForce") is True:   # 同 iPhone「宽屏强制启用」：没验证过的也铺满，画面被拉伸会自动退回 16:9
        env["S2A_WIDEN_VERIFIED"] = "1"
        env["S2A_WIDESCREEN"] = "1"
    if saved.get("metalfx") is True and caps.get("direct") == "1":
        env["S2A_SCALING_FILTER"] = "MetalFX"   # 直连上屏 MetalFX 空间放大（graphics/nvn/gfx/nvn/nvn_present_fx.inc）
    if not s["widescreen"]:
        env["S2A_WIDESCREEN"] = "0"       # 宽屏关：宿主宽屏开关（直连线 widen/setting；不用判定局专用的 S2A_WIDEN_VERIFIED，它会盖掉名单门）
    if not s["fps60"]:
        env["S2A_FPS60"] = "0"            # 60 帧关：回游戏自己的帧率（runtime/src/fps.c，帧率能耗线）
    return env
