// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// 首遇等待按预热清单分类（nvn_mtl.m fu_list_class，#1001）。
// 背景（11-14 0400486024DEC000 Mac 后期存档）：进场景不可跳过的首遇等待 14 次、大多 290–410 ms，要分清哪些是种子 / 预热已经接住、只是没轮到，
//   哪些是完全没见过，才能算两级建管线还剩多少收益。
// 期望（按「首遇的管线和清单 R 行是同一把种子键」手写，不由被测实现推出；不经 Metal）：
//   ① 本局还没读预热清单：-1
//   ② 清单有 A、B 两条 R 行：A、B 没被领走时都是 1（在清单里、没轮到），清单里没有的 C 是 0
//   ③ 预热工人按顺序领走第一行（A）：A 变 2（在编），B 仍 1
//   ④ 同一对着色器、只差颜色格式的管线不算在清单里（键要整把对上）：0
//   ⑤ 预热跑完（warm_urgent_end）之后：分类照旧（A 2、B 1、C 0）
#include "../gfx/nvn/nvn_mtl.m"

static unsigned checks, failures;
#define CHECK(c, ...) do { checks++; int good_ = !!(c); failures += !good_; printf("%s ", good_ ? "PASS" : "FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
static NvShader *sh(const char *key, int stage) { NvShader *s = [NvShader new]; strlcpy(s->key, key, sizeof s->key); s->stage = stage; return s; }
static pso_key key_fmt(uint32_t f) { pso_key k; memset(&k, 0, sizeof k); k.cfmt[0] = f; k.samples = 1; k.chmask = 0xf; return k; }
static WarmTask *row(NvShader *vs, NvShader *fs, const pso_key *k) {
    pso_skey sk; pso_skey_from(k, vs, fs, 0, NULL, &sk);
    WarmTask *t = [WarmTask new]; t->kind = 1; t->vkey = @(vs->key); t->fkey = @(fs->key); t->rs = [NSMutableArray arrayWithObject:[NSData dataWithBytes:&sk length:sizeof sk]]; return t;
}
int main(void) {
    @autoreleasepool {
        NvShader *va = sh("aaaaaaaaaaaaaaaa_b", 0), *fa = sh("bbbbbbbbbbbbbbbb_b", 1), *vb = sh("cccccccccccccccc_b", 0), *fb = sh("dddddddddddddddd_b", 1), *vc = sh("eeeeeeeeeeeeeeee_b", 0);
        pso_key k1 = key_fmt(0x25), k2 = key_fmt(0x3f);
        int c = fu_list_class(&k1, va, fa);
        CHECK(c == -1, "① 还没读预热清单：%d（期望 -1）", c);
        NSArray<WarmTask *> *tasks = @[row(va, fa, &k1), row(vb, fb, &k2)];
        warm_urgent_index(tasks);
        int a = fu_list_class(&k1, va, fa), b = fu_list_class(&k2, vb, fb), cc = fu_list_class(&k1, vc, fa);
        CHECK(a == 1 && b == 1 && cc == 0, "② 清单有 A、B：A %d、B %d（期望 1、1），清单外 C %d（期望 0）", a, b, cc);
        _Atomic long next = 0; long got = warm_pick(&next, (long)tasks.count);
        a = fu_list_class(&k1, va, fa); b = fu_list_class(&k2, vb, fb);
        CHECK(got == 0 && a == 2 && b == 1, "③ 工人领走第 %ld 行：A %d（期望 2）、B %d（期望 1）", got, a, b);
        int d = fu_list_class(&k2, va, fa);
        CHECK(d == 0, "④ 同一对着色器、颜色格式不同：%d（期望 0）", d);
        warm_urgent_end();
        a = fu_list_class(&k1, va, fa); b = fu_list_class(&k2, vb, fb); cc = fu_list_class(&k1, vc, fa);
        CHECK(a == 2 && b == 1 && cc == 0, "⑤ 预热结束后：A %d、B %d、C %d（期望 2、1、0）", a, b, cc);
    }
    printf("%s t_nvn_fu_class: %u checks %u failures\n", failures ? "✗" : "✓", checks, failures);
    return failures ? 1 : 0;
}
