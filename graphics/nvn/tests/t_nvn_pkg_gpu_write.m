// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// Production draw / rw-image dispatch, public CPU writes, normal texture binding,
// then independent Metal readback. Expected colors are hand-written constants.
#include "../gfx/nvn/nvn_mtl.m"
#include "../gfx/nvn/nvn_protos.h"
#include <signal.h>
extern void *h_nvnBootstrapLoader(const char *name);
int s2a_direct_fault(void *addr, int is_write, void *uctx);
static unsigned checks, failures;
#define REQUIRE(c) do { if (!(c)) { fprintf(stderr,"SETUP FAIL %s:%d %s\n",__FILE__,__LINE__,#c); exit(2); } } while (0)
#define CHECK(c, ...) do { checks++; int ok=!!(c); failures+=!ok; printf("%s ",ok?"PASS":"FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
static void *api(const char *name) { void *p=h_nvnBootstrapLoader(name); REQUIRE(p); return p; }
#define API(name) ((NVNT_##name)api(#name))
enum { W=256, H=256, POOL_SIZE=16<<20, TEX_OFF=5<<20, OUT_OFF=4<<20, TID=7 };
static NVNdevice device; static NVNqueue queue; static NVNmemoryPool pool;
static uint8_t *memory; static prog_t graphics, writer, reader;
static void fault(int s, siginfo_t *si, void *uc) { if (s2a_direct_fault(si->si_addr,1,uc)) return; signal(s,SIG_DFL); }
static void finish(void) { uint64_t f=nvn_be_sub_commit(); REQUIRE(nvn_be_wait(f,10000000000ull)); REQUIRE(!mtl_last_commit_error()); }
static NSString *const source=@"#include <metal_stdlib>\nusing namespace metal;\n"
"vertex float4 tri(uint i [[vertex_id]]) { const float2 p[3]={float2(-1,-1),float2(3,-1),float2(-1,3)}; return float4(p[i],0,1); }\n"
"fragment float4 red() { return float4(1,0,0,1); }\n"
"struct Write { texture2d<float,access::write> image [[id(0)]]; };\n"
"kernel void wr(constant Write &a [[buffer(0)]],uint2 p [[thread_position_in_grid]]) { a.image.write(float4(1,0,0,1),p); }\n"
"struct Read { texture2d<float,access::read> image [[id(0)]]; device float4 *out [[id(1)]]; };\n"
"kernel void rd(constant Read &a [[buffer(0)]]) { a.out[0]=a.image.read(uint2(0)); a.out[1]=a.image.read(uint2(255)); }\n";
static NvShader *shader(id<MTLLibrary> lib, NSString *name, int stage, NSDictionary *reflection) {
    NvShader *s=[NvShader new]; s->stage=stage; s->fn=[lib newFunctionWithName:name]; REQUIRE(s->fn);
    strlcpy(s->key,stage==0?"aaaaaaaaaaaaaaaa_b":stage==1?"bbbbbbbbbbbbbbbb_b":[name isEqualToString:@"wr"]?"cccccccccccccccc_b":"dddddddddddddddd_b",sizeof s->key); s->rf.pos_attr=-1; s->rf.has_user=1;
    if (reflection) { char error[512]; REQUIRE(!nvn_refl_from_json(reflection,&s->rf,error,sizeof error));
        s->ae=[s->fn newArgumentEncoderWithBufferIndex:0]; REQUIRE(s->ae); s->arg_len=s->ae.encodedLength; }
    return s;
}
static void program(prog_t *p, NvProg *np) { *p=(prog_t){.magic=MAGIC('P','R','O','G'),.serial=nvn_new_serial()}; serial_put(p->serial,np); }
static void read_bound(void) {
    nvn_be_sub_begin(); state_reset(); S.tex_base=0; S.cprog=&reader; S.tex[5][0]=TID;
    S.ssbo[5][0]=(p_bind_buf){.addr=(uint64_t)(uintptr_t)(memory+OUT_OFF),.size=32}; args_dirty_all();
    p_dispatch d={.x=1,.y=1,.z=1}; dispatch(&d); finish();
}
static void pixels(const char *kind, const char *step, id<MTLTexture> image, int changed) {
    id<MTLBuffer> b=[g_dev newBufferWithLength:W*H*4 options:MTLResourceStorageModeShared]; REQUIRE(b);
    id<MTLCommandQueue> q=[g_dev newCommandQueue]; REQUIRE(q); id<MTLCommandBuffer> cb=[q commandBuffer];
    id<MTLBlitCommandEncoder> enc=[cb blitCommandEncoder]; REQUIRE(enc);
    [enc copyFromTexture:image sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(W,H,1)
        toBuffer:b destinationOffset:0 destinationBytesPerRow:W*4 destinationBytesPerImage:W*H*4];
    [enc endEncoding]; [cb commit]; [cb waitUntilCompleted]; REQUIRE(cb.status==MTLCommandBufferStatusCompleted);
    const uint8_t *a=b.contents, *z=a+(W*H-1)*4;
    CHECK(a[0]==(changed?0:255) && a[1]==(changed?255:0) && a[2]==0 && a[3]==255,
        "%s %s CPU-page (0,0) expected=(%d,%d,0,255), actual=(%u,%u,%u,%u)",kind,step,changed?0:255,changed?255:0,a[0],a[1],a[2],a[3]);
    CHECK(z[0]==255 && z[1]==0 && z[2]==0 && z[3]==255,
        "%s %s untouched-page (255,255) expected=(255,0,0,255), actual=(%u,%u,%u,%u)",kind,step,z[0],z[1],z[2],z[3]);
}
static void run(id<MTLLibrary> lib, int compute) {
    const char *kind=compute?"rw-image dispatch":"color-attachment draw";
    NVNtextureBuilder b; API(nvnTextureBuilderSetDefaults)(&b); API(nvnTextureBuilderSetDevice)(&b,&device);
    API(nvnTextureBuilderSetTarget)(&b,NVN_TEXTURE_TARGET_2D); API(nvnTextureBuilderSetSize2D)(&b,W,H);
    API(nvnTextureBuilderSetFormat)(&b,NVN_FORMAT_RGBA8); API(nvnTextureBuilderSetLevels)(&b,1);
    API(nvnTextureBuilderSetFlags)(&b,NVN_TEXTURE_FLAGS_IMAGE); API(nvnTextureBuilderSetStorage)(&b,&pool,TEX_OFF+compute*(1<<20));
    NVNtexture texture={0}; REQUIRE(API(nvnTextureInitialize)(&texture,&b)); tex_t *t=(tex_t *)&texture;
    uint8_t *blue=malloc(W*H*4); REQUIRE(blue); for (int i=0;i<W*H;i++) { blue[4*i]=0; blue[4*i+1]=0; blue[4*i+2]=255; blue[4*i+3]=255; }
    NVNcopyRegion full={0,0,0,W,H,1}; API(nvnTextureWriteTexels)(&texture,NULL,&full,blue); free(blue);
    nvn_be_reg_tex(TID,t,NULL); read_bound();
    REQUIRE([tex_get(t->serial) isKindOfClass:[NvPkg class]]);
    const float *out=(float *)(memory+OUT_OFF); REQUIRE(out[2]==1 && out[6]==1);
    REQUIRE(tex_mtl(t));
    // Separate host pages are essential: current tracking promises page granularity.
    printf("CASE %s: initialized packed blue texture; host page size=%d\n",kind,getpagesize());
    nvn_be_sub_begin(); state_reset(); args_dirty_all();
    if (compute) { S.cprog=&writer; S.img[5][0]=TID; p_dispatch d={.x=W,.y=H,.z=1}; dispatch(&d); }
    else { S.prog=&graphics; rp_flush(); memset(&R.rt,0,sizeof R.rt); R.rt.n=1; R.rt.color[0]=t; R.has_rt=1; R.has_sc=0; g_pso_dirty=1;
        p_draw d={.prim=NVN_DRAW_PRIMITIVE_TRIANGLES,.count=3,.instances=1}; draw(&d,NULL); }
    finish();
    // Attachment binding may upgrade the packed image to render-target usage.
    // Read that live resource without invoking another texture resolution.
    REQUIRE([tex_get(t->serial) isKindOfClass:[NvPkg class]]);
    NvPkg *packed=tex_get(t->serial); REQUIRE(packed->tex);
    pixels(kind,"GPU writer",packed->tex,0);
    REQUIRE([tex_get(t->serial) isKindOfClass:[NvPkg class]]);
    const uint8_t green[4]={0,255,0,255}; NVNcopyRegion one={0,0,0,1,1,1}; API(nvnTextureWriteTexels)(&texture,NULL,&one,green);
    read_bound(); out=(float *)(memory+OUT_OFF);
    CHECK(out[0]==0 && out[1]==1 && out[2]==0 && out[3]==1,"%s normal sampled CPU pixel expected green, actual=(%.1f,%.1f,%.1f,%.1f)",kind,out[0],out[1],out[2],out[3]);
    CHECK(out[4]==1 && out[5]==0 && out[6]==0 && out[7]==1,"%s normal sampled untouched page expected red, actual=(%.1f,%.1f,%.1f,%.1f)",kind,out[4],out[5],out[6],out[7]);
    pixels(kind,"after CPU write / normal binding",tex_mtl(t),1);
    API(nvnTextureFinalize)(&texture);
}
static void run_scaled(void) {
    enum { SH=192 };
    NVNtextureBuilder b; API(nvnTextureBuilderSetDefaults)(&b); API(nvnTextureBuilderSetDevice)(&b,&device);
    API(nvnTextureBuilderSetTarget)(&b,NVN_TEXTURE_TARGET_2D); API(nvnTextureBuilderSetSize2D)(&b,W,SH);
    API(nvnTextureBuilderSetFormat)(&b,NVN_FORMAT_RGBA8); API(nvnTextureBuilderSetLevels)(&b,1);
    API(nvnTextureBuilderSetFlags)(&b,NVN_TEXTURE_FLAGS_COMPRESSIBLE); API(nvnTextureBuilderSetStorage)(&b,&pool,TEX_OFF);
    NVNtexture texture={0}; REQUIRE(API(nvnTextureInitialize)(&texture,&b)); tex_t *t=(tex_t *)&texture;
    uint8_t *blue=malloc(W*SH*4); REQUIRE(blue); for (int i=0;i<W*SH;i++) { blue[4*i]=0; blue[4*i+1]=0; blue[4*i+2]=255; blue[4*i+3]=255; }
    NVNcopyRegion full={0,0,0,W,SH,1}; API(nvnTextureWriteTexels)(&texture,NULL,&full,blue); free(blue);
    nvn_be_sub_begin(); state_reset(); args_dirty_all(); S.prog=&graphics;
    rp_flush(); memset(&R.rt,0,sizeof R.rt); R.rt.n=1; R.rt.color[0]=t; R.has_rt=1; R.has_sc=0; g_pso_dirty=1;
    p_draw d={.prim=NVN_DRAW_PRIMITIVE_TRIANGLES,.count=3,.instances=1}; draw(&d,NULL); finish();
    REQUIRE([tex_get(t->serial) isKindOfClass:[NvPkg class]]); NvPkg *pk=tex_get(t->serial);
    REQUIRE(pk->tex.width==W/2 && pk->tex.height==SH/2 && rs_state(pk->tex)==RS_SCALED);
    REQUIRE(pk->pgpu);   // The production attachment write must reach the partial-upload branch.
    printf("CASE scaled packed attachment: GPU storage=%lux%lu, game=%dx%d\n",(unsigned long)pk->tex.width,(unsigned long)pk->tex.height,W,SH);
    const uint8_t green[4]={0,255,0,255}; NVNcopyRegion one={0,0,0,1,1,1}; API(nvnTextureWriteTexels)(&texture,NULL,&one,green);
    nvn_be_sub_begin(); state_reset(); id<MTLTexture> image=tex_mtl(t); REQUIRE(image); finish();
    int full_size=image.width==W && image.height==SH;
    CHECK(full_size,"scaled CPU update rebuilds game-size storage expected=%dx%d actual=%lux%lu",W,SH,(unsigned long)image.width,(unsigned long)image.height);
    if (full_size) {
        id<MTLBuffer> buf=[g_dev newBufferWithLength:W*SH*4 options:MTLResourceStorageModeShared]; REQUIRE(buf);
        id<MTLCommandQueue> q=[g_dev newCommandQueue]; id<MTLCommandBuffer> cb=[q commandBuffer]; id<MTLBlitCommandEncoder> enc=[cb blitCommandEncoder]; REQUIRE(enc);
        [enc copyFromTexture:image sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(W,SH,1)
            toBuffer:buf destinationOffset:0 destinationBytesPerRow:W*4 destinationBytesPerImage:W*SH*4];
        [enc endEncoding]; [cb commit]; [cb waitUntilCompleted]; REQUIRE(cb.status==MTLCommandBufferStatusCompleted);
        const uint8_t *p=buf.contents, *z=p+(W*SH-1)*4;
        CHECK(p[0]==0 && p[1]==255 && p[2]==0 && p[3]==255,"scaled CPU-page expected green actual=(%u,%u,%u,%u)",p[0],p[1],p[2],p[3]);
        CHECK(z[0]==0 && z[1]==0 && z[2]==255 && z[3]==255,"scaled whole re-upload uses guest bytes expected blue actual=(%u,%u,%u,%u)",z[0],z[1],z[2],z[3]);
    }
    nvn_be_sub_begin(); state_reset(); args_dirty_all(); S.prog=&graphics;
    rp_flush(); memset(&R.rt,0,sizeof R.rt); R.rt.n=1; R.rt.color[0]=t; R.has_rt=1; R.has_sc=0; g_pso_dirty=1;
    draw(&d,NULL); finish(); pk=tex_get(t->serial);
    CHECK(pk->tex.width==W/2 && pk->tex.height==SH/2,"rebuilt packed attachment scales on next pass expected=%dx%d actual=%lux%lu",W/2,SH/2,(unsigned long)pk->tex.width,(unsigned long)pk->tex.height);
    API(nvnTextureFinalize)(&texture);
}
int main(int argc, char **argv) { @autoreleasepool {
    REQUIRE(argc==1 || (argc==2 && !strcmp(argv[1],"scaled"))); int scaled=argc==2;
    setenv("S2A_RES_SCALE",scaled?"0.5":"1",1);
    setvbuf(stdout,NULL,_IOLBF,0); setenv("DM_PSO_ARCHIVE","0",1);
    struct sigaction sa={0}; sa.sa_sigaction=fault; sa.sa_flags=SA_SIGINFO; sigaction(SIGBUS,&sa,NULL); sigaction(SIGSEGV,&sa,NULL);
    NVNdeviceBuilder db; API(nvnDeviceBuilderSetDefaults)(&db); REQUIRE(API(nvnDeviceInitialize)(&device,&db));
    NVNqueueBuilder qb; API(nvnQueueBuilderSetDefaults)(&qb); API(nvnQueueBuilderSetDevice)(&qb,&device); REQUIRE(API(nvnQueueInitialize)(&queue,&qb));
    REQUIRE(!posix_memalign((void **)&memory,65536,POOL_SIZE)); memset(memory,0,POOL_SIZE);
    NVNmemoryPoolBuilder pb; API(nvnMemoryPoolBuilderSetDefaults)(&pb); API(nvnMemoryPoolBuilderSetDevice)(&pb,&device);
    API(nvnMemoryPoolBuilderSetStorage)(&pb,memory,POOL_SIZE); API(nvnMemoryPoolBuilderSetFlags)(&pb,NVN_MEMORY_POOL_FLAGS_CPU_UNCACHED|NVN_MEMORY_POOL_FLAGS_GPU_CACHED); REQUIRE(API(nvnMemoryPoolInitialize)(&pool,&pb));
    const op_hdr empty={OP_END,sizeof(op_hdr)}; nvn_be_exec((const uint8_t *)&empty); printf("BACKEND Metal%d\n",g_m3?3:4);
    NSError *error=nil; id<MTLLibrary> lib=[g_dev newLibraryWithSource:source options:nil error:&error]; if(!lib) fprintf(stderr,"MSL %s\n",error.localizedDescription.UTF8String); REQUIRE(lib);
    NvProg *gp=[NvProg new]; gp->sh[0]=shader(lib,@"tri",0,nil); gp->sh[1]=shader(lib,@"red",1,nil); gp->sh[1]->rf.color_known=1; gp->sh[1]->rf.color_mask=1; program(&graphics,gp);
    NvProg *wp=[NvProg new]; wp->sh[5]=shader(lib,@"wr",5,@{@"route":@"B",@"threadgroup_size":@[@1,@1,@1],@"bindings":@[@{@"kind":@"img",@"reg":@0,@"arg_id":@0,@"rw":@YES,@"msl_type":@"texture2d<float, access::write>"}]}); program(&writer,wp);
    NvProg *rp=[NvProg new]; rp->sh[5]=shader(lib,@"rd",5,@{@"route":@"B",@"threadgroup_size":@[@1,@1,@1],@"bindings":@[@{@"kind":@"tex",@"reg":@0,@"arg_id":@0,@"msl_type":@"texture2d<float>"},@{@"kind":@"sbuf",@"reg":@0,@"arg_id":@1,@"rw":@YES}]}); program(&reader,rp);
    if (scaled) run_scaled(); else { run(lib,0); run(lib,1); }
    printf("%s t_nvn_pkg_gpu_write%s: %u checks, %u failures\n",failures?"✗":"✓",scaled?" scaled":"",checks,failures); return failures?1:0;
} }
