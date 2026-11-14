// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// 同遍深度采样的快照存储（nvn_mtl.m dsnap_storage）按形状复用，不在几张深度之间来回重建。
// 背景（11-13 0400486024DEC000 Mac 读档进场景）：每帧先后采样 768x432、1536x864 两张深度（Depth32Float_Stencil8），快照存储只留一份，
//   每帧来回整张新建、旧的推迟释放，每 300 帧新建 900 张、436 MB，常驻集跟着进出 900 次。
// 期望（按「同形状的存储可原地复用」这条现有语义手写，不由被测实现推出）：
//   ① 两种形状交替取 50 轮：每种形状每次拿到的都是第一次那张（不新建）
//   ② 四种形状交替取 20 轮：同上
//   ③ 六种形状（两两同宽、高不同）轮流取：每次拿到的存储格式、宽高、级数都和请求一致，且在常驻集里（份数不够时换掉旧的，不能拿错形状）
//   ④ 带级偏移（tl = 1）：按那一级的宽高建，和直接请求那一级尺寸的是同一张
#include "../gfx/nvn/nvn_mtl.m"

static unsigned checks, failures;
#define REQUIRE(c) do { if (!(c)) { fprintf(stderr, "SETUP FAIL %s:%d %s\n", __FILE__, __LINE__, #c); exit(2); } } while (0)
#define CHECK(c, ...) do { checks++; int good_ = !!(c); failures += !good_; printf("%s ", good_ ? "PASS" : "FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
static const MTLPixelFormat DS = MTLPixelFormatDepth32Float_Stencil8;
static int resident(id<MTLTexture> t) { pthread_mutex_lock(&g_res_mu); int r = [g_res_objects containsObject:t]; pthread_mutex_unlock(&g_res_mu); return r; }
static int rounds_same(const NSUInteger (*sz)[2], int n, int rounds) {   // n 种形状交替取 rounds 轮，换过存储的次数
    __strong id<MTLTexture> first[8]; int changed = 0;
    for (int i = 0; i < n; i++) first[i] = dsnap_storage(DS, sz[i][0], sz[i][1], 0, 1);
    for (int r = 0; r < rounds; r++) for (int i = 0; i < n; i++) changed += dsnap_storage(DS, sz[i][0], sz[i][1], 0, 1) != first[i];
    return changed;
}
int main(void) {
    @autoreleasepool {
        char map[] = "/tmp/t_dsnap_map_XXXXXX"; int fd = mkstemp(map); REQUIRE(fd >= 0); close(fd);
        char user[] = "/tmp/t_dsnap_user_XXXXXX"; fd = mkstemp(user); REQUIRE(fd >= 0); close(fd);
        setenv("DM_NVN_SHMAP", map, 1); setenv("DM_PSO_USER", user, 1); setenv("DM_PSO_ARCHIVE", "0", 1);
        mtl_init(); REQUIRE(g_dev);
        static const NSUInteger two[2][2] = {{768, 432}, {1536, 864}};
        int c = rounds_same(two, 2, 50);
        CHECK(c == 0, "① 768x432 / 1536x864 交替取 50 轮：存储不换（换了 %d 次，期望 0）", c);
        static const NSUInteger four[4][2] = {{683, 384}, {341, 192}, {170, 96}, {1536, 864}};
        c = rounds_same(four, 4, 20);
        CHECK(c == 0, "② 四种形状交替取 20 轮：存储不换（换了 %d 次，期望 0）", c);
        static const NSUInteger six[6][2] = {{64, 32}, {64, 16}, {32, 64}, {32, 8}, {48, 48}, {48, 24}};
        int bad = 0, cold = 0;
        for (int r = 0; r < 5; r++) for (int i = 0; i < 6; i++) { id<MTLTexture> t = dsnap_storage(DS, six[i][0], six[i][1], 0, 1);
            bad += !t || t.pixelFormat != DS || t.width != six[i][0] || t.height != six[i][1] || t.mipmapLevelCount != 1; cold += t && !resident(t); }
        CHECK(!bad && !cold, "③ 六种形状轮流取：形状不对 %d 次、不在常驻集 %d 次（期望都 0）", bad, cold);
        id<MTLTexture> a = dsnap_storage(DS, 200, 100, 1, 1), b = dsnap_storage(DS, 100, 50, 0, 1);
        CHECK(a && a.width == 100 && a.height == 50 && a == b, "④ 级偏移 1：按 100x50 建（得 %lux%lu），与直接请求 100x50 同一张（%s）",
            (unsigned long)a.width, (unsigned long)a.height, a == b ? "是" : "否");
        unlink(map); unlink(user);
    }
    printf("%s t_nvn_dsnap_slots: %u checks %u failures\n", failures ? "✗" : "✓", checks, failures);
    return failures ? 1 : 0;
}
