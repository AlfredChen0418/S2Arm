// 游戏库（shell S2ArmShellHost）调整虚拟手柄布局的宿主实现，接口见 s2a_shell.h「虚拟手柄布局」。
//   11-07 维护者：按住游戏 / 点设置 → 虚拟手柄布局 → 二级页拖动按键（范例图上找位置），按游戏存，不套用到其他游戏。
//   与游戏内（vpad.m 拖动编辑）同一份数据：Documents/games/<游戏号>/VPadLayouts（多款容器选游戏后的 $S2A_DOCS，runtime/src/launchctx.c），
//   外观设置仍在 App 级 Documents/VPadLayouts。只在主线程调（库界面）。
#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#include <stdlib.h>
#include <string.h>
#import "vpad_layout.h"

// 游戏号进路径：同 launchctx.c id_ok —— 只收 [A-Za-z0-9._-]、不以 . 开头、≤127 字
static BOOL gid_ok(const char *g) {
    if (!g || !*g || g[0] == '.' || strlen(g) > 127) return NO;
    for (const char *p = g; *p; p++) if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '.' || *p == '_' || *p == '-')) return NO;
    return YES;
}
static S2AVPadLayoutStore *store_for(const char *gid) {
    if (!gid_ok(gid)) return nil;
    const char *t = getenv("S2A_SHELL_DOCS");   // 只给单测：Mac 上 Documents 是用户真目录，单测指到临时目录
    NSURL *docs = t && *t ? [NSURL fileURLWithPath:@(t) isDirectory:YES] : [NSFileManager.defaultManager URLsForDirectory:NSDocumentDirectory inDomains:NSUserDomainMask].firstObject;
    NSURL *game = [[docs URLByAppendingPathComponent:@"games" isDirectory:YES] URLByAppendingPathComponent:@(gid) isDirectory:YES];
    return [[S2AVPadLayoutStore alloc] initWithDirectory:[game URLByAppendingPathComponent:@"VPadLayouts" isDirectory:YES]
                                       settingsDirectory:[docs URLByAppendingPathComponent:@"VPadLayouts" isDirectory:YES]];
}
// 要编辑的底稿：本款「我的布局」能读就用它，否则按这块安全区现算默认布局
static S2AVPadLayout *base_for(S2AVPadLayoutStore *s, CGRect safe, BOOL *hasMine) {
    *hasMine = NO;
    for (S2AVPadLayout *l in s.layouts) if ([l.identifier isEqualToString:S2AVPadLayoutMineIdentifier]) { *hasMine = YES; return l; }
    return [S2AVPadLayout defaultForSafeRect:safe];
}

char *s2a_shell_vpad_layout_json(const char *game_id, double safe_w, double safe_h) {
    @autoreleasepool {
        S2AVPadLayoutStore *s = store_for(game_id); if (!s || !(safe_w > 0) || !(safe_h > 0)) return NULL;
        BOOL hasMine; S2AVPadLayout *b = base_for(s, CGRectMake(0, 0, safe_w, safe_h), &hasMine);
        NSString *sel = s.selectedIdentifier;
        NSString *kind = [sel isEqualToString:S2AVPadLayoutMineIdentifier] ? @"mine" : [sel isEqualToString:S2AVPadLayoutDefaultIdentifier] ? @"default"
                       : [sel isEqualToString:S2AVPadLayoutTouchIdentifier] ? @"touch" : @"other";
        NSDictionary *doc = @{@"selected": kind, @"hasMine": @(hasMine), @"holdLock": @(s.holdLockEnabled), @"buttons": b.buttons};
        NSData *d = [NSJSONSerialization dataWithJSONObject:doc options:0 error:nil]; if (!d) return NULL;
        char *out = malloc(d.length + 1); if (!out) return NULL;
        memcpy(out, d.bytes, d.length); out[d.length] = 0; return out;
    }
}

int s2a_shell_vpad_save(const char *game_id, const char *centers_json, double safe_w, double safe_h) {
    @autoreleasepool {
        S2AVPadLayoutStore *s = store_for(game_id); if (!s || !centers_json || !(safe_w > 0) || !(safe_h > 0)) return 0;
        NSData *d = [NSData dataWithBytes:centers_json length:strlen(centers_json)];
        id c = [NSJSONSerialization JSONObjectWithData:d options:0 error:nil]; if (![c isKindOfClass:NSDictionary.class]) return 0;
        CGRect safe = CGRectMake(0, 0, safe_w, safe_h); BOOL hasMine; NSError *e;
        S2AVPadLayout *m = [base_for(s, safe, &hasMine) layoutWithButtonCenters:c safeRect:safe identifier:S2AVPadLayoutMineIdentifier name:@"我的布局" error:&e];
        BOOL ok = m && [s replaceLayout:m error:&e] && [s selectIdentifier:S2AVPadLayoutMineIdentifier error:&e] && [s markManual:&e];
        if (!ok) NSLog(@"S2Arm 游戏库：保存 %s 的按键位置失败：%@", game_id, e.localizedDescription);
        return ok ? 1 : 0;
    }
}

int s2a_shell_vpad_set_hold_lock(const char *game_id, int enabled) {
    @autoreleasepool {
        S2AVPadLayoutStore *s = store_for(game_id); if (!s) return 0;
        NSError *e; BOOL ok = [s setHoldLockEnabled:enabled != 0 error:&e];
        if (!ok) NSLog(@"S2Arm 游戏库：%s 保存按住锁定失败：%@", game_id, e.localizedDescription);
        return ok ? 1 : 0;
    }
}

int s2a_shell_vpad_select(const char *game_id, const char *layout_id) {
    @autoreleasepool {
        S2AVPadLayoutStore *s = store_for(game_id); if (!s || !layout_id) return 0;
        NSString *i = !strcmp(layout_id, "mine") ? S2AVPadLayoutMineIdentifier : !strcmp(layout_id, "default") ? S2AVPadLayoutDefaultIdentifier : nil;
        if (!i) return 0;
        NSError *e; BOOL ok = [s selectIdentifier:i error:&e] && [s markManual:&e];
        if (!ok) NSLog(@"S2Arm 游戏库：%s 切到 %s 失败：%@", game_id, layout_id, e.localizedDescription);
        return ok ? 1 : 0;
    }
}
