// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// 两级建管线的耗时探针（nvn_mtl.m spec_probe，#1001 第二步）。
// 背景（11-14 0400486024DEC000 Mac）：首遇等待全是「清单里没有」的管线，SetShaders 平均比首次绘制早 78 秒；要定两级建值不值得做，得知道
//   片元输出留空的基础管线、从它特化、完整建各要多久。探针在后台编首遇管线之前用 Metal 4 编译器把同一份描述建三遍计时。
// 期望（按 Metal 4 特化接口与探针口径手写，不由被测实现推出）：
//   ① 缺省（没设 DM_NVN_PSO_SPEC_PROBE）：探针不开
//   ② 开了、顶点 / 片元都是库主入口、两个颜色目标（RGBA8 混合开 + RGBA16F 混合关）：基础管线、特化、完整建都成功，计 1 条、跳过 0、失败 0，三段耗时都 > 0
//   ③ 片元换成同库里别的入口（变体入口，不是 fs->fn）：不建，跳过计 1，已测条数不变
//   ④ 再测一条：已测 2 条，基础 / 完整谁先建轮换（第 1 条基础先、第 2 条完整先），两条都成功
//   ⑤ 片元声明了函数常量（这款游戏的片元多数如此）：照样建成、计第 3 条，进程不 abort
//   ⑥ 片元主入口已按 shader_load 换成特化后的函数（函数常量表为空）：同样要套特化描述，不 abort
#include "../gfx/nvn/nvn_mtl.m"

static unsigned checks, failures;
#define REQUIRE(c) do { if (!(c)) { fprintf(stderr, "SETUP FAIL %s:%d %s\n", __FILE__, __LINE__, #c); exit(2); } } while (0)
#define CHECK(c, ...) do { checks++; int good_ = !!(c); failures += !good_; printf("%s ", good_ ? "PASS" : "FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
static NSString *const src = @"#include <metal_stdlib>\nusing namespace metal;\n"
"struct V { float4 p [[position]]; };\n"
"vertex V vmain(uint i [[vertex_id]]) { const float2 q[3] = {float2(-1,-1),float2(3,-1),float2(-1,3)}; V o; o.p = float4(q[i],0,1); return o; }\n"
"struct O { float4 a [[color(0)]]; float4 b [[color(1)]]; };\n"
"fragment O fmain(V v [[stage_in]]) { O o; o.a = float4(v.p.x * 0.001, 0, 0, 1); o.b = float4(0, v.p.y * 0.001, 0, 1); return o; }\n"
"fragment O fother(V v [[stage_in]]) { O o; o.a = float4(1, 0, 0, 1); o.b = float4(0, 1, 0, 1); return o; }\n"
"constant bool kAt [[function_constant(0)]];\nconstant bool kAtOn = is_function_constant_defined(kAt) ? kAt : false;\n"
"fragment O fconst(V v [[stage_in]]) { O o; o.a = float4(kAtOn ? 1 : 0, 0, 0, 1); o.b = float4(0, 1, 0, 1); return o; }\n";
static NvShader *sh(id<MTLLibrary> lib, NSString *name, const char *key, int stage) { NvShader *s = [NvShader new]; s->fn = [lib newFunctionWithName:name]; REQUIRE(s->fn); s->plib = lib; strlcpy(s->key, key, sizeof s->key); s->stage = stage; return s; }
static MTLRenderPipelineDescriptor *desc(NvShader *vs, id<MTLFunction> f) {
    MTLRenderPipelineDescriptor *d = [MTLRenderPipelineDescriptor new]; d.vertexFunction = vs->fn; d.fragmentFunction = f;
    d.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA8Unorm; d.colorAttachments[0].blendingEnabled = YES;
    d.colorAttachments[0].sourceRGBBlendFactor = d.colorAttachments[0].sourceAlphaBlendFactor = MTLBlendFactorSourceAlpha;
    d.colorAttachments[0].destinationRGBBlendFactor = d.colorAttachments[0].destinationAlphaBlendFactor = MTLBlendFactorOneMinusSourceAlpha;
    d.colorAttachments[1].pixelFormat = MTLPixelFormatRGBA16Float; d.colorAttachments[1].writeMask = MTLColorWriteMaskRed | MTLColorWriteMaskGreen;
    return d;
}
int main(void) {
    @autoreleasepool {
        char map[] = "/tmp/t_specp_map_XXXXXX"; int fd = mkstemp(map); REQUIRE(fd >= 0); close(fd);
        char user[] = "/tmp/t_specp_user_XXXXXX"; fd = mkstemp(user); REQUIRE(fd >= 0); close(fd);
        setenv("DM_NVN_SHMAP", map, 1); setenv("DM_PSO_USER", user, 1); setenv("DM_PSO_ARCHIVE", "0", 1); unsetenv("DM_NVN_PSO_SPEC_PROBE");
        mtl_init(); REQUIRE(g_dev);
        if (g_m3) { printf("✓ t_nvn_spec_probe: 0 checks 0 failures（Metal 3 设备没有特化接口，不测）\n"); return 0; }
        CHECK(!spec_probe_on(), "① 缺省探针不开");
        setenv("DM_NVN_PSO_SPEC_PROBE", "1", 1);
        // spec_probe_on 读一次就缓存：这里直接调探针本身（开关只决定 pso_pend_kick 调不调它）
        NSError *e = nil; id<MTLLibrary> lib = [g_dev newLibraryWithSource:src options:nil error:&e]; REQUIRE(lib);
        NvShader *vs = sh(lib, @"vmain", "aaaaaaaaaaaaaaaa_b", 0), *fs = sh(lib, @"fmain", "bbbbbbbbbbbbbbbb_b", 1);
        spec_probe(desc(vs, fs->fn), vs, fs);
        CHECK(g_sp.n == 1 && !g_sp.skipped && !g_sp.failed && g_sp.base_ns && g_sp.spec_ns && g_sp.full_ns,
              "② 主入口两个目标：已测 %llu、跳过 %llu、失败 %llu（期望 1、0、0），基础 %.2f / 特化 %.2f / 完整 %.2f ms（期望都 > 0）",
              (unsigned long long)g_sp.n, (unsigned long long)g_sp.skipped, (unsigned long long)g_sp.failed, g_sp.base_ns / 1e6, g_sp.spec_ns / 1e6, g_sp.full_ns / 1e6);
        id<MTLFunction> other = [lib newFunctionWithName:@"fother"]; REQUIRE(other);
        spec_probe(desc(vs, other), vs, fs);
        CHECK(g_sp.n == 1 && g_sp.skipped == 1, "③ 片元是变体入口：已测 %llu（期望 1）、跳过 %llu（期望 1）", (unsigned long long)g_sp.n, (unsigned long long)g_sp.skipped);
        NvShader *fs2 = sh(lib, @"fother", "cccccccccccccccc_b", 1);
        spec_probe(desc(vs, fs2->fn), vs, fs2);
        CHECK(g_sp.n == 2 && !g_sp.failed, "④ 再测一条（完整先建）：已测 %llu（期望 2）、失败 %llu（期望 0）", (unsigned long long)g_sp.n, (unsigned long long)g_sp.failed);
        NvShader *fs3 = sh(lib, @"fconst", "dddddddddddddddd_b", 1);   // 声明了函数常量（缺省取默认值），这款游戏的片元多数如此
        spec_probe(desc(vs, fs3->fn), vs, fs3);
        CHECK(g_sp.n == 3 && !g_sp.failed, "⑤ 片元声明了函数常量：已测 %llu（期望 3）、失败 %llu（期望 0），不 abort", (unsigned long long)g_sp.n, (unsigned long long)g_sp.failed);
        NvShader *fs4 = sh(lib, @"fconst", "eeeeeeeeeeeeeeee_b", 1);   // 同 shader_load：主入口换成特化后的函数（它的函数常量表是空的）
        fs4->fn = [lib newFunctionWithName:@"fconst" constantValues:[MTLFunctionConstantValues new] error:&e]; REQUIRE(fs4->fn);
        spec_probe(desc(vs, fs4->fn), vs, fs4);
        CHECK(g_sp.n == 4 && !g_sp.failed, "⑥ 片元主入口已特化：已测 %llu（期望 4）、失败 %llu（期望 0），不 abort", (unsigned long long)g_sp.n, (unsigned long long)g_sp.failed);
        unlink(map); unlink(user);
    }
    printf("%s t_nvn_spec_probe: %u checks %u failures\n", failures ? "✗" : "✓", checks, failures);
    return failures ? 1 : 0;
}
