// 摇杆模式（ios/vpad_layout.m）：固定 / 浮动、灵敏度、固定位置的存储和灵敏度计算。期望按规则手写
#import <Foundation/Foundation.h>
#include <math.h>
#import "../../ios/vpad_layout.h"
static int bad;
#define CK(c, ...) do { if (c) printf("✓ " __VA_ARGS__); else { printf("✗ " __VA_ARGS__); bad++; } printf("\n"); } while (0)
static BOOL near(CGFloat a, CGFloat b) { return fabs(a - b) < 1e-9; }
int main(void) { @autoreleasepool {
    NSURL *d = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString] isDirectory:YES]; NSError *e;
    S2AVPadLayoutStore *s = [[S2AVPadLayoutStore alloc] initWithDirectory:d];
    CK(!s.stickFixed && s.stickSensitivity == 1 && near([s stickCenter:0].x, .12) && near([s stickCenter:1].x, .63), "① 缺省：浮动、灵敏度 1、缺省位置");
    CK(![s setStickFixed:YES sensitivity:2.1 error:&e] && ![s setStickFixed:YES sensitivity:0.4 error:&e] && ![s setStickCenter:CGPointMake(1.2, 0) side:0 error:&e], "② 超范围拒绝：灵敏度 2.1 / 0.4、位置 1.2");
    CK([s setStickFixed:YES sensitivity:1.5 error:&e] && [s setStickCenter:CGPointMake(.2, .7) side:1 error:&e], "③ 合法值写入");
    S2AVPadLayoutStore *r = [[S2AVPadLayoutStore alloc] initWithDirectory:d];
    CK(r.stickFixed && r.stickSensitivity == 1.5 && near([r stickCenter:1].y, .7) && near([r stickCenter:0].x, .12), "④ 重开仍在，另一侧不变");
    [@"{坏" writeToURL:[d URLByAppendingPathComponent:@".stick_mode.json"] atomically:YES encoding:NSUTF8StringEncoding error:nil]; r = [[S2AVPadLayoutStore alloc] initWithDirectory:d];
    CK(!r.stickFixed && r.stickSensitivity == 1, "⑤ 文件损坏回默认");
    CGPoint o = S2AVPadStickSensitivity(CGPointMake(.3, .4), 1);
    CK(near(o.x, .3) && near(o.y, .4), "⑥ 灵敏度 1 不改变输入");
    o = S2AVPadStickSensitivity(CGPointMake(.3, .4), 2);
    CK(near(o.x, .6) && near(o.y, .8), "⑦ 灵敏度 2：(.3,.4) → (.6,.8)，长度刚好 1");
    o = S2AVPadStickSensitivity(CGPointMake(.6, .8), 2);
    CK(near(o.x, .6) && near(o.y, .8), "⑧ 超出单位圆钳回，方向不变");
    o = S2AVPadStickSensitivity(CGPointMake(.6, 0), .5);
    CK(near(o.x, .3) && near(o.y, 0), "⑨ 灵敏度 0.5：减半");
    if (bad) printf("t_vpad_stick_mode：%d 项不过\n", bad); else printf("✓ t_vpad_stick_mode：9 项\n");
    return bad != 0;
} }
