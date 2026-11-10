// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// RGB9E5 渲染目标经 R32UI 视图写（04008E001F098000 自己打包 HDR 颜色：用整数视图写进 RGB9E5 贴图）。
// 改前：RGB9E5 贴图建时不带 PixelFormatView 用途，attach 时 R32UI 视图挂不上（rt_view_fmt 退回基底格式，红行「cannot be attached」），
//   这一遍按 RGB9E5 画，打包的字被当成浮点写，读回整片偏红（0x7e020080 读成 (65408, 0, 0) 一类）。
// 期望（按 Metal 视图规则与 NVN 视图语义手写，不由被测实现推出）：
//   ① RGB9E5（非预排布）贴图带 PixelFormatView 用途
//   ② 挂 R32UI 视图：附件是 R32Uint 视图，不退回 RGB9E5
//   ③ 经该附件写 uint 0x7e020080，基底 RGB9E5 贴图读回原字 0x7e020080（逐位相同）
// 反证：tex_desc 去掉 RGB9E5 那一行（= 改前）→ ① ② 红，③ 没有 R32Uint 附件可写、记红
#include "../gfx/nvn/nvn_mtl.m"

static unsigned checks, failures;
#define REQUIRE(c) do { if (!(c)) { fprintf(stderr, "SETUP FAIL %s:%d %s\n", __FILE__, __LINE__, #c); exit(2); } } while (0)
#define CHECK(c, ...) do { checks++; int good_ = !!(c); failures += !good_; printf("%s ", good_ ? "PASS" : "FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)

static NSString *const source = @"#include <metal_stdlib>\nusing namespace metal;\n"
    "vertex float4 tri(uint i [[vertex_id]]) { const float2 p[3] = {float2(-1, -1), float2(3, -1), float2(-1, 3)}; return float4(p[i], 0, 1); }\n"
    "fragment uint4 word() { return uint4(0x7e020080u, 0, 0, 0); }\n";

int main(void) { @autoreleasepool {
    setvbuf(stdout, NULL, _IOLBF, 0); alarm(60);
    mtl_init(); printf("BACKEND %s\n", g_m3 ? "Metal3" : "Metal4");
    uint8_t *mem = NULL; REQUIRE(!posix_memalign((void **)&mem, 65536, 1 << 20)); memset(mem, 0, 1 << 20);
    pool_t *pool = calloc(1, sizeof *pool); REQUIRE(pool);
    *pool = (pool_t){.magic = MAGIC('P','O','O','L'), .flags = NVN_MEMORY_POOL_FLAGS_CPU_NO_ACCESS | NVN_MEMORY_POOL_FLAGS_GPU_CACHED, .mem = mem, .size = 1 << 20};
    tex_t t; memset(&t, 0, sizeof t); t.magic = MAGIC('T','E','X',0); t.serial = nvn_new_serial();
    t.d = (nvn_texdesc){.target = 1, .format = NVN_FORMAT_RGB9E5F, .width = 4, .height = 4, .depth = 1, .levels = 1};
    t.pool = pool; t.off = 0; t.mem = pool->mem; nvn_tex_layout(&t.d, &t.L); nvn_be_tex_init(&t);
    id<MTLTexture> base = tex_mtl(&t); REQUIRE(base); REQUIRE(base.pixelFormat == MTLPixelFormatRGB9E5Float);
    CHECK(base.usage & MTLTextureUsagePixelFormatView, "① RGB9E5 贴图带 PixelFormatView 用途：期望 有 → 真结果 %s", base.usage & MTLTextureUsagePixelFormatView ? "有" : "没有");

    view_t v; memset(&v, 0, sizeof v); v.set_format = 1; v.format = NVN_FORMAT_R32UI;
    MTLRenderPassColorAttachmentDescriptor *a = [MTLRenderPassColorAttachmentDescriptor new];
    attach(a, &t, &v, 1);
    MTLPixelFormat got = a.texture ? a.texture.pixelFormat : MTLPixelFormatInvalid;
    CHECK(got == MTLPixelFormatR32Uint, "② 挂 R32UI 视图：期望 附件格式 %lu (R32Uint) → 真结果 %lu", (unsigned long)MTLPixelFormatR32Uint, (unsigned long)got);

    uint32_t word = 0;
    if (got == MTLPixelFormatR32Uint) {
        id<MTLDevice> dev = base.device; id<MTLCommandQueue> q = [dev newCommandQueue];
        NSError *e = nil; id<MTLLibrary> lib = [dev newLibraryWithSource:source options:nil error:&e]; REQUIRE(lib);
        MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new]; pd.vertexFunction = [lib newFunctionWithName:@"tri"]; pd.fragmentFunction = [lib newFunctionWithName:@"word"];
        pd.colorAttachments[0].pixelFormat = MTLPixelFormatR32Uint; id<MTLRenderPipelineState> ps = [dev newRenderPipelineStateWithDescriptor:pd error:&e]; REQUIRE(ps);
        MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
        rp.colorAttachments[0].texture = a.texture; rp.colorAttachments[0].loadAction = MTLLoadActionDontCare; rp.colorAttachments[0].storeAction = MTLStoreActionStore;
        id<MTLBuffer> out = [dev newBufferWithLength:4 * 4 * 4 options:MTLResourceStorageModeShared];
        id<MTLCommandBuffer> cb = [q commandBuffer]; id<MTLRenderCommandEncoder> en = [cb renderCommandEncoderWithDescriptor:rp];
        [en setRenderPipelineState:ps]; [en drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3]; [en endEncoding];
        id<MTLBlitCommandEncoder> bl = [cb blitCommandEncoder];
        [bl copyFromTexture:base sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(4, 4, 1) toBuffer:out destinationOffset:0 destinationBytesPerRow:16 destinationBytesPerImage:64];
        [bl endEncoding]; [cb commit]; [cb waitUntilCompleted]; REQUIRE(cb.status == MTLCommandBufferStatusCompleted);
        word = ((const uint32_t *)out.contents)[5];   // (1, 1)
    }
    CHECK(word == 0x7e020080u, "③ 经 R32UI 视图写 0x7e020080，基底 RGB9E5 读回：期望 0x7e020080 → 真结果 %#x", word);

    printf("%s t_nvn_rt_rgb9e5_view: %u checks %u failures; backend %s\n", failures ? "✗" : "✓", checks, failures, g_m3 ? "Metal3" : "Metal4");
    return failures ? 1 : 0;
} }
