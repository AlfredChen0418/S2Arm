// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// Real public NVN reports and conditional clears; an unsignaled Metal event
// holds submission A on the GPU while the CPU encodes submission B.
#ifndef NVN_RC_SOURCE
#define NVN_RC_SOURCE "../gfx/nvn/nvn_mtl.m"
#endif
#include NVN_RC_SOURCE
#include "nvn_protos.h"
extern void *h_nvnBootstrapLoader(const char *name);
static unsigned checks, failures;
#define REQUIRE(x) do { if (!(x)) { fprintf(stderr,"SETUP FAIL %s:%d %s\n",__FILE__,__LINE__,#x); exit(2); } } while (0)
#define CHECK(x, ...) do { checks++; int ok=!!(x); failures+=!ok; printf("%s ",ok?"PASS":"FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
static void *load(const char *name) { void *p=h_nvnBootstrapLoader(name); REQUIRE(p); return p; }
#define API(name) ((NVNT_##name)load(#name))
enum { SEG=65536, BYTES=8*SEG, N=4 };
static NVNdevice dev;
static NVNqueue queue;
static NVNmemoryPool pool;
static NVNtexture texture;
static NVNcommandBuffer commands[4];
static uint8_t *storage;
static uint64_t *record;
static NVNbufferAddress record_address;
static void begin(int i) { API(nvnCommandBufferBeginRecording)(&commands[i]); }
static uint64_t submit(int i) {
    NVNcommandHandle h=API(nvnCommandBufferEndRecording)(&commands[i]); REQUIRE(h);
    API(nvnQueueSubmitCommands)(&queue,1,&h); return nvn_be_last_fence();
}
static void wait_fence(uint64_t f) { REQUIRE(nvn_be_wait(f,10000000000ull)); REQUIRE(!mtl_last_commit_error()); }
static void clear_record(int i, int conditional, float r, float b) {
    const NVNtexture *targets[]={&texture};
    API(nvnCommandBufferSetRenderTargets)(&commands[i],1,targets,NULL,NULL,NULL);
    if (conditional) API(nvnCommandBufferSetRenderEnableConditional)(&commands[i],NVN_CONDITIONAL_RENDER_MODE_RENDER_IF_NOT_EQUAL,record_address);
    const float color[]={r,0,b,1}; API(nvnCommandBufferClearColor)(&commands[i],0,color,NVN_CLEAR_COLOR_MASK_RGBA);
    API(nvnCommandBufferSetRenderEnable)(&commands[i],1);
}
static void pixels(const char *phase, unsigned r, unsigned b) {
    id<MTLTexture> image=tex_mtl((const tex_t *)&texture); REQUIRE(image);
    id<MTLBuffer> out=[g_dev newBufferWithLength:N*256 options:MTLResourceStorageModeShared]; REQUIRE(out);
    memset(out.contents,0xa5,N*256);
    id<MTLCommandQueue> q=[g_dev newCommandQueue]; REQUIRE(q);
    id<MTLCommandBuffer> cb=[q commandBuffer]; id<MTLBlitCommandEncoder> e=[cb blitCommandEncoder];
    [e copyFromTexture:image sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(N,N,1)
        toBuffer:out destinationOffset:0 destinationBytesPerRow:256 destinationBytesPerImage:N*256];
    [e endEncoding]; [cb commit]; [cb waitUntilCompleted]; REQUIRE(cb.status==MTLCommandBufferStatusCompleted);
    const uint8_t *p=out.contents; unsigned wrong=0;
    for (unsigned y=0;y<N;y++) for (unsigned x=0;x<N;x++) {
        const uint8_t *v=p+y*256+x*4; if(v[0]!=r || v[1] || v[2]!=b || v[3]!=255) wrong++;
    }
    CHECK(!wrong,"%s: 16 GPU pixels expected=(%u,0,%u,255), first=(%u,%u,%u,%u), wrong=%u",phase,r,b,p[0],p[1],p[2],p[3],wrong);
}
int main(void) { @autoreleasepool {
    setvbuf(stdout,NULL,_IOLBF,0); setenv("DM_PSO_ARCHIVE","0",1);
    NVNdeviceBuilder db; API(nvnDeviceBuilderSetDefaults)(&db); REQUIRE(API(nvnDeviceInitialize)(&dev,&db));
    NVNqueueBuilder qb; API(nvnQueueBuilderSetDefaults)(&qb); API(nvnQueueBuilderSetDevice)(&qb,&dev); REQUIRE(API(nvnQueueInitialize)(&queue,&qb));
    REQUIRE(posix_memalign((void **)&storage,SEG,BYTES)==0); memset(storage,0,BYTES);
    NVNmemoryPoolBuilder pb; API(nvnMemoryPoolBuilderSetDefaults)(&pb); API(nvnMemoryPoolBuilderSetDevice)(&pb,&dev);
    API(nvnMemoryPoolBuilderSetStorage)(&pb,storage,BYTES);
    API(nvnMemoryPoolBuilderSetFlags)(&pb,NVN_MEMORY_POOL_FLAGS_CPU_UNCACHED|NVN_MEMORY_POOL_FLAGS_GPU_CACHED); REQUIRE(API(nvnMemoryPoolInitialize)(&pool,&pb));
    record=(uint64_t *)(storage+6*SEG); record_address=API(nvnMemoryPoolGetBufferAddress)(&pool)+6*SEG;
    NVNtextureBuilder tb; API(nvnTextureBuilderSetDefaults)(&tb); API(nvnTextureBuilderSetDevice)(&tb,&dev);
    API(nvnTextureBuilderSetTarget)(&tb,NVN_TEXTURE_TARGET_2D); API(nvnTextureBuilderSetSize2D)(&tb,N,N);
    API(nvnTextureBuilderSetFormat)(&tb,NVN_FORMAT_RGBA8); API(nvnTextureBuilderSetStorage)(&tb,&pool,0); REQUIRE(API(nvnTextureInitialize)(&texture,&tb));
    static uint8_t control[4][4096];
    for(int i=0;i<4;i++) { REQUIRE(API(nvnCommandBufferInitialize)(&commands[i],&dev)); API(nvnCommandBufferAddControlMemory)(&commands[i],control[i],sizeof control[i]); API(nvnCommandBufferAddCommandMemory)(&commands[i],&pool,(i+1)*SEG,SEG); }
    printf("BACKEND %s render_cond=%d\n",g_m3?"Metal3":"Metal4",DM_GAME_RENDER_COND); REQUIRE(DM_GAME_RENDER_COND);
    begin(0); clear_record(0,0,0,1); wait_fence(submit(0)); pixels("initial blue",0,255);
    record[0]=1; record[2]=0;
    id<MTLSharedEvent> gate=[g_dev newSharedEvent]; REQUIRE(gate);
    nvn_be_queue_wait_event((__bridge void *)gate,1);
    begin(1); API(nvnCommandBufferReportCounter)(&commands[1],NVN_COUNTER_TYPE_TIMESTAMP_TOP,record_address); uint64_t a=submit(1);
    CHECK(!nvn_be_wait(a,0) && record[0]==1,"A blocked: timestamp has not landed, CPU counter remains 1");
    begin(2); clear_record(2,1,1,0); uint64_t b=submit(2);
    CHECK(!nvn_be_wait(b,0),"B submitted while A remains blocked");
    gate.signaledValue=1; wait_fence(b);
    CHECK(record[0]==0,"A completed: timestamp counter is 0"); pixels("in-flight timestamp suppresses red clear",0,255);
    record[0]=1; // A is complete: the CPU's new request must win over old reports.
    begin(3); clear_record(3,1,1,0); wait_fence(submit(3)); pixels("completed report then CPU reset enables red clear",255,0);
    printf("%s t_nvn_rc_pending: %u checks, %u failures\n",failures?"✗":"✓",checks,failures); return failures!=0;
} }
