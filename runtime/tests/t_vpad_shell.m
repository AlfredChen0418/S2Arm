// 游戏库调整虚拟手柄布局的宿主接口（ios/vpad_shell.m，s2a_shell.h「虚拟手柄布局」）。跑：make native-test-vpad_custom
// 期望值手算（区域 832×419，默认布局 A 键中心 x = 0.905·832 + 47 → 比例 0.905 + 47/832）：
//   ① g1 没有自定义：selected default、hasMine false、16 键、A 键 x = 0.905 + 47/832
//   ② 存 {"A":[416,209.5]} → 1；games/g1/VPadLayouts/mine.json 在；再读 selected mine、hasMine true、A 键 x 0.5 y 0.5
//   ③ g2 不受影响：selected default、hasMine false
//   ④ 选 default → 1；selected default、hasMine true（文件保留），底稿仍是 mine（A 键 x 0.5）
//   ⑤ 游戏号 "../x" / ".h"：读返回 NULL、写返回 0
//   ⑥ 游戏内（vpad.m 同目录）打开 g1：选 mine 后读到 A 键 x 0.5
#import <Foundation/Foundation.h>
#include <stdlib.h>
#import "../../ios/vpad_layout.h"
char *s2a_shell_vpad_layout_json(const char *game_id, double safe_w, double safe_h);
int s2a_shell_vpad_save(const char *game_id, const char *centers_json, double safe_w, double safe_h);
int s2a_shell_vpad_select(const char *game_id, const char *layout_id);
int s2a_shell_vpad_set_hold_lock(const char *game_id, int enabled);
static int bad;
#define CK(c, ...) do { if (c) printf("✓ "); else { printf("✗ "); bad++; } printf(__VA_ARGS__); printf("\n"); } while (0)
static NSDictionary *rd(const char *g) { char *j = s2a_shell_vpad_layout_json(g, 832, 419); if (!j) return nil;
    NSDictionary *d = [NSJSONSerialization JSONObjectWithData:[NSData dataWithBytes:j length:strlen(j)] options:0 error:nil]; free(j); return d; }
static NSDictionary *btn(NSDictionary *d, NSString *i) { for (NSDictionary *b in d[@"buttons"]) if ([b[@"id"] isEqualToString:i]) return b; return nil; }
static BOOL near(double a, double b) { return fabs(a - b) < 1e-9; }
int main(void) {
    @autoreleasepool {
        NSString *root = [NSTemporaryDirectory() stringByAppendingPathComponent:[@"vpad_shell_" stringByAppendingString:NSUUID.UUID.UUIDString]];
        setenv("S2A_SHELL_DOCS", root.fileSystemRepresentation, 1);
        NSDictionary *d = rd("g1");
        CK(d && [d[@"selected"] isEqualToString:@"default"] && ![d[@"hasMine"] boolValue] && [d[@"buttons"] count] == 16, "① g1 默认：selected %s、hasMine %d、%lu 键", [d[@"selected"] UTF8String], [d[@"hasMine"] intValue], (unsigned long)[d[@"buttons"] count]);
        CK(near([btn(d, @"A")[@"x"] doubleValue], 0.905 + 47.0 / 832), "① A 键 x = 0.905 + 47/832（实得 %.6f）", [btn(d, @"A")[@"x"] doubleValue]);
        CK(s2a_shell_vpad_save("g1", "{\"A\":[416,209.5]}", 832, 419) == 1, "② 存「我的布局」成功");
        CK([NSFileManager.defaultManager fileExistsAtPath:[root stringByAppendingPathComponent:@"games/g1/VPadLayouts/mine.json"]], "② mine.json 在 games/g1/VPadLayouts");
        d = rd("g1");
        CK([d[@"selected"] isEqualToString:@"mine"] && [d[@"hasMine"] boolValue] && near([btn(d, @"A")[@"x"] doubleValue], 0.5) && near([btn(d, @"A")[@"y"] doubleValue], 0.5), "② 再读：selected %s、A 键 (%.3f, %.3f)", [d[@"selected"] UTF8String], [btn(d, @"A")[@"x"] doubleValue], [btn(d, @"A")[@"y"] doubleValue]);
        NSDictionary *d2 = rd("g2");
        CK([d2[@"selected"] isEqualToString:@"default"] && ![d2[@"hasMine"] boolValue], "③ g2 不受影响：selected %s、hasMine %d", [d2[@"selected"] UTF8String], [d2[@"hasMine"] intValue]);
        CK(s2a_shell_vpad_select("g1", "default") == 1, "④ 恢复默认成功");
        d = rd("g1");
        CK([d[@"selected"] isEqualToString:@"default"] && [d[@"hasMine"] boolValue] && near([btn(d, @"A")[@"x"] doubleValue], 0.5), "④ selected default、mine 保留、底稿仍是 mine（A x %.3f）", [btn(d, @"A")[@"x"] doubleValue]);
        CK(rd("../x") == nil && rd(".h") == nil && s2a_shell_vpad_save("../x", "{}", 832, 419) == 0 && s2a_shell_vpad_select("../x", "mine") == 0, "⑤ 不合规游戏号读写都拒绝");
        CK([rd("g1")[@"holdLock"] boolValue] && s2a_shell_vpad_set_hold_lock("g1", 0) == 1 && ![rd("g1")[@"holdLock"] boolValue] && [rd("g2")[@"holdLock"] boolValue] && s2a_shell_vpad_set_hold_lock("../x", 0) == 0, "⑦ 按住锁定缺省开；g1 关了不影响 g2；不合规游戏号拒绝");
        CK(s2a_shell_vpad_select("g1", "mine") == 1, "⑥ 选回 mine");
        S2AVPadLayoutStore *s = [[S2AVPadLayoutStore alloc] initWithDirectory:[NSURL fileURLWithPath:[root stringByAppendingPathComponent:@"games/g1/VPadLayouts"]]
                                                           settingsDirectory:[NSURL fileURLWithPath:[root stringByAppendingPathComponent:@"VPadLayouts"]]];
        S2AVPadLayout *l = [s selectedLayout:nil]; double ax = -1; for (NSDictionary *b in l.buttons) if ([b[@"id"] isEqualToString:@"A"]) ax = [b[@"x"] doubleValue];
        CK([l.identifier isEqualToString:S2AVPadLayoutMineIdentifier] && near(ax, 0.5), "⑥ 游戏内同目录读到 mine、A x %.3f", ax);
        [NSFileManager.defaultManager removeItemAtPath:root error:nil];
    }
    if (bad) printf("✗ t_vpad_shell：%d 项不通\n", bad); else printf("✓ t_vpad_shell：全部通过\n");
    return bad ? 1 : 0;
}
