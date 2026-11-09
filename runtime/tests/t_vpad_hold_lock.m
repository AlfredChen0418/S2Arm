// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// ZL / ZR 按住锁定（ios/vpad_layout.m）。时钟由测试给，期望手写：
//   发给游戏的按键 = 手指按着的 | locked，所以「松手前不改变已发按键状态」直接查这个并集。
#import "../../ios/vpad_layout.h"
static unsigned checks, bad;
#define CK(c) do { checks++; if (!(c)) { bad++; printf("FAIL line %d: %s\n", __LINE__, #c); } } while (0)
enum { ZL = 1u << 8, ZR = 1u << 9, A = 1u << 0, KEYS = ZL | ZR };
#define STEP(held, t) S2AVPadHoldLockStep(&s, (held), KEYS, (t), YES)
#define SENT(held) ((held) | s.locked)
int main(void) { @autoreleasepool {
    S2AVPadHoldLockState s = {0};
    // 按住不到 2 秒不切；到 2 秒才切，切的那一拍按键一直是按下
    CK(S2AVPadHoldLockDeadline(&s) == 0);
    CK(STEP(ZL, 10) == 0 && s.locked == 0 && SENT(ZL) == ZL);
    CK(S2AVPadHoldLockDeadline(&s) == 12);
    CK(STEP(ZL, 11.99) == 0 && s.locked == 0);
    CK(STEP(ZL, 12) == ZL && s.locked == ZL && SENT(ZL) == ZL);
    CK(S2AVPadHoldLockDeadline(&s) == 0);
    // 锁定那根手指松开：不解锁，按键继续按着
    CK(STEP(0, 12.5) == 0 && s.locked == ZL && SENT(0) == ZL);
    // 再点一下：落指不变，抬手才解锁
    CK(STEP(ZL, 20) == 0 && s.locked == ZL && SENT(ZL) == ZL);
    CK(S2AVPadHoldLockDeadline(&s) == 0);
    CK(STEP(ZL, 25) == 0 && s.locked == ZL);
    CK(STEP(0, 25.1) == ZL && s.locked == 0 && SENT(0) == 0);
    // 解锁后重新计满 2 秒
    CK(STEP(ZL, 30) == 0 && STEP(ZL, 31.9) == 0 && s.locked == 0);
    CK(STEP(0, 31.95) == 0 && s.locked == 0 && SENT(0) == 0);
    // 主线程卡顿、定时器没赶上：抬手时已满 2 秒照样锁
    CK(STEP(ZL, 40) == 0 && STEP(0, 42.5) == ZL && s.locked == ZL);
    CK(STEP(ZL, 43) == 0 && STEP(0, 43.1) == ZL && s.locked == 0);
    // ZL / ZR 各计各的；到期时间取最早没锁的
    CK(STEP(ZL, 50) == 0 && STEP(KEYS, 51) == 0 && S2AVPadHoldLockDeadline(&s) == 52);
    CK(STEP(KEYS, 52) == ZL && s.locked == ZL && S2AVPadHoldLockDeadline(&s) == 53);
    CK(STEP(KEYS, 53) == ZR && s.locked == KEYS && S2AVPadHoldLockDeadline(&s) == 0);
    CK(STEP(0, 53.1) == 0 && s.locked == KEYS);
    CK(STEP(ZR, 54) == 0 && STEP(0, 54.1) == ZR && s.locked == ZL);
    // 非 ZL / ZR 的键再久也不锁，且和已锁的键互不影响
    CK(STEP(A, 60) == 0 && STEP(A, 90) == 0 && s.locked == ZL && S2AVPadHoldLockDeadline(&s) == 0);
    CK(STEP(0, 91) == 0 && s.locked == ZL);
    // 关闭：立即清掉，不按「切换」上报（调用方不震）；关闭期间不计时
    CK(S2AVPadHoldLockStep(&s, ZL, KEYS, 100, NO) == 0 && s.locked == 0 && S2AVPadHoldLockDeadline(&s) == 0);
    CK(S2AVPadHoldLockStep(&s, ZL, KEYS, 110, NO) == 0 && s.locked == 0);
    // 关闭时按着、打开后按键仍在：从打开这一拍起算，不补计关闭期间
    CK(STEP(ZL, 120) == 0 && s.locked == 0 && S2AVPadHoldLockDeadline(&s) == 122);

    // 开关：按游戏存（directory），缺省开，不建文件；两款互不影响，也不写进 App 级目录
    NSFileManager *fm = NSFileManager.defaultManager; NSError *error = nil;
    NSURL *root = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString] isDirectory:YES];
    NSURL *g1 = [root URLByAppendingPathComponent:@"g1" isDirectory:YES], *g2 = [root URLByAppendingPathComponent:@"g2" isDirectory:YES], *app = [root URLByAppendingPathComponent:@"app" isDirectory:YES];
    S2AVPadLayoutStore *store = [[S2AVPadLayoutStore alloc] initWithDirectory:g1 settingsDirectory:app];
    CK(store.holdLockEnabled && ![fm fileExistsAtPath:g1.path]);
    CK([store setHoldLockEnabled:NO error:&error] && !store.holdLockEnabled);
    NSURL *file = [g1 URLByAppendingPathComponent:@".hold_lock.json"];
    CK([fm fileExistsAtPath:file.path] && ![fm fileExistsAtPath:[app URLByAppendingPathComponent:@".hold_lock.json"].path]);
    store = [[S2AVPadLayoutStore alloc] initWithDirectory:g1 settingsDirectory:app];
    CK(!store.holdLockEnabled && store.hapticFeedbackEnabled);
    CK([[S2AVPadLayoutStore alloc] initWithDirectory:g2 settingsDirectory:app].holdLockEnabled);
    CK([store setHoldLockEnabled:YES error:&error]);
    CK([[S2AVPadLayoutStore alloc] initWithDirectory:g1 settingsDirectory:app].holdLockEnabled);
    // 损坏：退回缺省开、列诊断、原文件保留；重新设置后诊断消失
    for (NSString *text in @[@"{}", @"{\"schemaVersion\":1,\"enabled\":1}", @"{\"schemaVersion\":2,\"enabled\":false}"]) {
        NSData *data = [text dataUsingEncoding:NSUTF8StringEncoding]; [data writeToURL:file atomically:YES];
        store = [[S2AVPadLayoutStore alloc] initWithDirectory:g1 settingsDirectory:app]; [store layouts];
        CK(store.holdLockEnabled && store.diagnostics.count == 1 && [[NSData dataWithContentsOfURL:file] isEqual:data]);
    }
    CK([store setHoldLockEnabled:NO error:&error]); [store layouts];
    CK(!store.holdLockEnabled && store.diagnostics.count == 0);
    // 写盘失败：内存值不变
    NSURL *blocked = [root URLByAppendingPathComponent:@"blocked"]; [NSData.data writeToURL:blocked atomically:YES];
    store = [[S2AVPadLayoutStore alloc] initWithDirectory:blocked settingsDirectory:app];
    CK(![store setHoldLockEnabled:NO error:&error] && store.holdLockEnabled);
    [fm removeItemAtURL:root error:nil];
    printf("%s t_vpad_hold_lock: %u checks, %u failures\n", bad ? "✗" : "✓", checks, bad);
    return bad != 0;
} }
