"""社区资源（pack/jobs/community.py，10-27 维护者 01:4x 口径）：宽屏 / 60 帧是宿主开关（缺省开、可关），版本不符走跨版本定位不判死，
画面同互斥组单选；Mac 启动按选择设 S2A_WIDESCREEN=0 / S2A_FPS60=0。期望按规则手写"""
import json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import community
from core import Failure

GID = "0100C2500FC20000"


class Ctx:
    def __init__(self, root):
        self.library, self.engine, self.r = root / "lib", root / "engine", {"gameId": GID}
    def begin(self, *a): pass


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding="utf-8")


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp()); self.c = Ctx(self.t)
        write(self.c.library / "catalog.json", json.dumps({"games": [{"id": GID, "packages": [
            {"type": "base", "version": 0, "display": "1.0.0", "builds": {"main": "AAAA"}},
            {"type": "update", "version": 1 << 16, "display": "4.1.0", "builds": {}}]}]}))
        write(self.c.engine / "graphics/widen/tables/verified.tsv", "# 注释\n程序号\t构建号\t款\t登记日期\t依据\n" + GID + "\taaaa\tspl3\t2026-10-27\t实测\n")
        write(self.c.engine / "graphics/community/other_gfx.tsv", "程序号\t版本\t编号\t名称\t互斥组\t已验证\t来源\n"
              f"{GID}\t4.1.0\tres1080\t1080p\tres\t0\t社区A\n{GID}\t4.1.0\tres900\t900p\tres\t0\t社区A\n{GID}\t4.1.0\tnoblur\t关动态模糊\t\t1\t社区B\n{GID}\t3.0.0\tfov\tFOV 加大\t\t0\t社区C\n")
        cd = self.t / "cp"; os.environ["S2A_COMMUNITY_DIR"] = str(cd)
        os.environ["S2A_COMMUNITY_PATCHES"] = "1"   # 下面三例按内部开发 / 测试包口径（列画面补丁）；玩家包缺省见 test_player_pack_no_graphics
        write(cd / "fps/full/x/INDEX.tsv", "game\ttitle_id\tversion\tbuild_id\ttype\ttarget_fps\tsource\tin_pack\tsha256\n"
              f"Spl\t{GID}\t1.1.1\t\tpchtxt\t60\tfpsrepo\tyes\tabc\nSpl\t{GID}\t4.1.0\t\tpchtxt\t30\tother30\tyes\tdef\n")
        write(cd / "uw/full/y/INDEX.tsv", "game\ttitle_id\tversion\tbuild_id\taspect\tkind\tui_fix\tsource\tin_pack\tsha256\n"
              f"Spl\t{GID}\t4.1.0\t\t21:9\tpchtxt\tyes\tuwrepo\tyes\t111\n")

    def test_defaults_and_sources(self):
        r = community.listing(self.c)
        ws, fps = r["widescreen"], r["fps60"]
        self.assertEqual((ws["available"], ws["verified"], ws["enabled"], ws["sources"], ws["relocate"]), (True, True, True, ["uwrepo"], False))
        self.assertEqual((fps["available"], fps["verified"], fps["enabled"], fps["sources"], fps["relocate"]), (True, False, True, ["fpsrepo"], True))   # 1.1.1≠4.1.0：跨版本定位、不判死；30 帧那条不算
        g = {it["id"]: it for it in r["graphics"]}
        self.assertEqual((g["gfx:noblur"]["enabled"], g["gfx:res1080"]["enabled"], g["gfx:fov"]["relocate"]), (True, False, True))

    def test_save_and_launch_env(self):
        self.assertEqual(community.launch_env(self.c, GID), {})                             # 缺省都开：不设
        self.c.r.update(widescreen=False, fps60=False, graphics={"gfx:res1080": True})
        community.save(self.c)
        self.assertEqual(community.launch_env(self.c, GID), {"S2A_WIDESCREEN": "0", "S2A_FPS60": "0"})
        self.c.r = {"gameId": GID, "graphics": {"gfx:res900": True}}
        r = community.save(self.c); g = {it["id"]: it for it in r["graphics"]}
        self.assertEqual((g["gfx:res900"]["enabled"], g["gfx:res1080"]["enabled"]), (True, False))   # 互斥组单选
        self.assertEqual((r["widescreen"]["enabled"], r["fps60"]["enabled"]), (False, False))       # 没带的项不变
        self.c.r = {"gameId": GID, "graphics": {"nope": True}}
        with self.assertRaises(Failure): community.save(self.c)

    def test_player_pack_no_graphics(self):
        # 10-30 维护者：玩家包不带上游补丁字节（S2A_COMMUNITY_PATCHES 没设）→ 画面补丁不列、不能存；宽屏 / 60 帧（我方能力）照常
        os.environ.pop("S2A_COMMUNITY_PATCHES", None)
        r = community.listing(self.c)
        self.assertEqual((r["graphics"], r["widescreen"]["available"], r["fps60"]["available"]), ([], True, True))
        self.c.r = {"gameId": GID, "graphics": {"gfx:res1080": True}}
        with self.assertRaises(Failure): community.save(self.c)

    def test_no_data(self):
        os.environ["S2A_COMMUNITY_DIR"] = str(self.t / "none")
        for f in ("graphics/community/other_gfx.tsv", "graphics/widen/tables/verified.tsv"): (self.c.engine / f).unlink()
        r = community.listing(self.c)
        self.assertEqual((r["widescreen"]["available"], r["fps60"]["available"], r["widescreen"]["enabled"], r["graphics"]), (False, False, False, []))



class MacDisplayTests(unittest.TestCase):
    """Mac 设置页：可用与否和缺省跟这款的能力清单走，只存玩家明确选过的。期望按规则手写"""
    def setUp(self):
        self.t = Path(tempfile.mkdtemp()); self.c = Ctx(self.t)
        write(self.c.library / "catalog.json", json.dumps({"games": [{"id": GID, "packages": []}]}))

    def caps(self, text="widen\t0\t# 缺省关\nfps60\t1\t# 开\ndirect\t1\t# 直连\n"):
        write(self.c.library / "games" / GID / "current/s2a_caps.tsv", "# 注释\n" + text)

    def save(self, **change):
        self.c.r = {"gameId": GID, **change}
        community.save(self.c)
        self.c.r = {"gameId": GID}

    def mac(self):
        return community.mac_settings(self.c, GID)

    def test_defaults_follow_the_caps(self):
        self.caps()
        m = self.mac()
        self.assertEqual((m["fps60"]["available"], m["fps60"]["enabled"]), (True, True))
        self.assertEqual((m["widenForce"]["available"], m["widenForce"]["enabled"]), (True, False))
        self.assertEqual((m["metalfx"]["available"], m["metalfx"]["enabled"]), (True, False))
        self.assertEqual(community.launch_env(self.c, GID), {})

    def test_game_without_a_mac_build_says_why(self):
        m = self.mac()
        for key in ("fps60", "widenForce", "metalfx"):
            self.assertFalse(m[key]["available"]); self.assertEqual(m[key]["reason"], community.NO_RUNTIME)

    def test_missing_capabilities_give_their_own_reason(self):
        self.caps("widen\t0\ndirect\t0\n")
        m = self.mac()
        self.assertEqual(m["fps60"]["reason"], community.NO_FPS60)
        self.assertEqual(m["metalfx"]["reason"], community.NO_DIRECT)
        self.assertTrue(m["widenForce"]["available"])

    def test_metalfx_reaches_launch_and_turns_back_off(self):
        self.caps()
        self.save(metalfx=True)
        self.assertEqual(community.launch_env(self.c, GID), {"S2A_SCALING_FILTER": "MetalFX"})
        self.assertTrue(self.mac()["metalfx"]["enabled"])
        self.save(metalfx=False)
        self.assertEqual(community.launch_env(self.c, GID), {})

    def test_metalfx_is_not_set_for_a_game_without_direct(self):
        self.caps()
        self.save(metalfx=True)
        self.caps("fps60\t1\ndirect\t0\n")
        self.assertEqual(community.launch_env(self.c, GID), {})

    def test_force_widescreen_matches_iphone(self):
        self.caps()
        self.save(widenForce=True)
        self.assertEqual(community.launch_env(self.c, GID), {"S2A_WIDEN_VERIFIED": "1", "S2A_WIDESCREEN": "1"})

    def test_fps60_off_and_manual_on_over_a_red_game(self):
        self.caps()
        self.save(fps60=False)
        self.assertEqual(community.launch_env(self.c, GID), {"S2A_FPS60": "0"})
        self.assertFalse(self.mac()["fps60"]["enabled"])
        self.caps("fps60\t0\t# 判红\ndirect\t1\n")
        self.save(fps60=True)
        self.assertEqual(community.launch_env(self.c, GID), {"S2A_FPS60": "1"})
        self.assertTrue(self.mac()["fps60"]["enabled"])

    def test_changing_one_switch_does_not_decide_the_others(self):
        # 判红的款：改 MetalFX 不能顺手把 60 帧存成开
        self.caps("fps60\t0\t# 判红\ndirect\t1\n")
        self.save(metalfx=True)
        self.assertFalse(self.mac()["fps60"]["enabled"])
        self.assertEqual(community.launch_env(self.c, GID), {"S2A_SCALING_FILTER": "MetalFX"})

    def test_old_file_written_by_the_previous_panel_is_not_a_manual_choice(self):
        self.caps("fps60\t0\t# 判红\ndirect\t1\n")
        write(self.c.library / "games" / GID / "community.json", json.dumps({"schema": 2, "widescreen": True, "fps60": True, "enabled": {}}))
        self.assertFalse(self.mac()["fps60"]["enabled"])
        self.assertEqual(community.launch_env(self.c, GID), {})

if __name__ == "__main__":
    unittest.main()
