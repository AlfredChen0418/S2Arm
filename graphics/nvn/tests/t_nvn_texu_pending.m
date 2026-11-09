// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// A: public UpdateUniformBuffer writes a red-texture handle behind a real GPU
// event wait. B: production TEXU dispatch reads it, without a B-local write.
// Hand-written expectation: red. The initial handle selects blue. Shared output
// is written by the real compute shader, not by a CPU emulation of sampling.
#ifndef NVN_TEXU_SOURCE
#define NVN_TEXU_SOURCE "../gfx/nvn/nvn_mtl.m"
#endif
#include NVN_TEXU_SOURCE
#include "../gfx/nvn/nvn_protos.h"
#include <signal.h>
#include <dispatch/dispatch.h>
extern void *h_nvnBootstrapLoader(const char *name);
int s2a_direct_fault(void *addr, int is_write, void *uctx);
int dm_nvn_request_stop(void);
#define REQUIRE(c) do { if (!(c)) { fprintf(stderr,"SETUP FAIL %s:%d %s\n",__FILE__,__LINE__,#c); exit(2); } } while (0)
static unsigned checks, failures;
#define CHECK(c, ...) do { checks++; int ok=!!(c); failures+=!ok; printf("%s ",ok?"PASS":"FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
static void *api(const char *name) { void *p=h_nvnBootstrapLoader(name); REQUIRE(p); return p; }
#define API(name) ((NVNT_##name)api(#name))
enum { MEM=16<<20, CMD=1<<20, HANDLE_OFF=4<<20, OUT_OFF=5<<20, BLUE_OFF=6<<20, RED_OFF=7<<20, OTHER_OFF=8<<20, BLUE_ID=9, RED_ID=10 };
static NVNdevice device; static NVNqueue queue; static NVNmemoryPool pool;
static uint8_t *memory; static uint64_t handle_addr; static float *output;
static prog_t program;
static void fault(int s, siginfo_t *si, void *uc) { if (s2a_direct_fault(si->si_addr,1,uc)) return; signal(s,SIG_DFL); }
static uint32_t cpu_handle(void) { uint32_t h; const uint8_t *p=addr_cpu(handle_addr,4); REQUIRE(p); memcpy(&h,p,4); return h; }
static uint64_t consume_begin(void) {
    nvn_be_sub_begin(); state_reset(); S.cprog=&program; S.tex_base=0;
    S.ubo[5][3]=(p_bind_buf){.addr=handle_addr,.size=256};
    S.ssbo[5][0]=(p_bind_buf){.addr=(uint64_t)(uintptr_t)output,.size=16}; args_dirty_all();
    // Merely binding the UBO must not add a pending write of its handle.
    REQUIRE(!pend_hits(handle_addr,4)); REQUIRE(!g_nubd);
    return g_sub_serial;
}
static uint64_t consume_end(void) { p_dispatch d={.x=1,.y=1,.z=1}; dispatch(&d); return nvn_be_sub_commit(); }
static void finish(uint64_t fence) { REQUIRE(nvn_be_wait(fence,10000000000ull)); REQUIRE(!mtl_last_commit_error()); }
static void texture(NVNtexture *t, int offset, unsigned id, const uint8_t color[4]) {
    NVNtextureBuilder b; API(nvnTextureBuilderSetDefaults)(&b); API(nvnTextureBuilderSetDevice)(&b,&device);
    API(nvnTextureBuilderSetTarget)(&b,NVN_TEXTURE_TARGET_2D); API(nvnTextureBuilderSetSize2D)(&b,1,1);
    API(nvnTextureBuilderSetFormat)(&b,NVN_FORMAT_RGBA8); API(nvnTextureBuilderSetLevels)(&b,1);
    API(nvnTextureBuilderSetStorage)(&b,&pool,offset); REQUIRE(API(nvnTextureInitialize)(t,&b));
    NVNcopyRegion r={0,0,0,1,1,1}; API(nvnTextureWriteTexels)(t,NULL,&r,color);
    nvn_be_reg_tex(id,(tex_t *)t,NULL);
}
int main(int argc, char **argv) { @autoreleasepool {
    int cancel=argc==2 && !strcmp(argv[1],"--cancel");
    int current=argc==2 && !strcmp(argv[1],"--inline");
    int later=argc==2 && !strcmp(argv[1],"--later"); REQUIRE(argc==1 || cancel || current || later);
    setvbuf(stdout,NULL,_IOLBF,0); setenv("DM_PSO_ARCHIVE","0",1);
    struct sigaction sa={0}; sa.sa_sigaction=fault; sa.sa_flags=SA_SIGINFO; sigaction(SIGBUS,&sa,NULL); sigaction(SIGSEGV,&sa,NULL);
    NVNdeviceBuilder db; API(nvnDeviceBuilderSetDefaults)(&db); REQUIRE(API(nvnDeviceInitialize)(&device,&db));
    NVNqueueBuilder qb; API(nvnQueueBuilderSetDefaults)(&qb); API(nvnQueueBuilderSetDevice)(&qb,&device); REQUIRE(API(nvnQueueInitialize)(&queue,&qb));
    REQUIRE(!posix_memalign((void **)&memory,65536,MEM)); memset(memory,0,MEM);
    NVNmemoryPoolBuilder pb; API(nvnMemoryPoolBuilderSetDefaults)(&pb); API(nvnMemoryPoolBuilderSetDevice)(&pb,&device);
    API(nvnMemoryPoolBuilderSetStorage)(&pb,memory,MEM); API(nvnMemoryPoolBuilderSetFlags)(&pb,NVN_MEMORY_POOL_FLAGS_CPU_UNCACHED|NVN_MEMORY_POOL_FLAGS_GPU_CACHED);
    REQUIRE(API(nvnMemoryPoolInitialize)(&pool,&pb));
    uint64_t ga=API(nvnMemoryPoolGetBufferAddress)(&pool); handle_addr=nvn_ga2ca(ga+HANDLE_OFF); output=(float *)(memory+OUT_OFF);
    const op_hdr empty={OP_END,sizeof(op_hdr)}; nvn_be_exec((const uint8_t *)&empty); printf("BACKEND Metal%d\n",g_m3?3:4);
    NVNtexture blue={0},red={0}; const uint8_t bc[4]={0,0,255,255},rc[4]={255,0,0,255};
    texture(&blue,BLUE_OFF,BLUE_ID,bc); texture(&red,RED_OFF,RED_ID,rc);
    NSError *error=nil; id<MTLLibrary> lib=[g_dev newLibraryWithSource:@"#include <metal_stdlib>\nusing namespace metal;\n"
        "struct Args { texture2d<float,access::read> t [[id(0)]]; device float4 *out [[id(1)]]; };\n"
        "kernel void rd(constant Args &a [[buffer(0)]]) { a.out[0]=a.t.read(uint2(0)); }\n" options:nil error:&error];
    if(!lib) fprintf(stderr,"MSL %s\n",error.localizedDescription.UTF8String); REQUIRE(lib);
    NvShader *cs=[NvShader new]; cs->stage=5; cs->fn=[lib newFunctionWithName:@"rd"]; REQUIRE(cs->fn);
    cs->ae=[cs->fn newArgumentEncoderWithBufferIndex:0]; REQUIRE(cs->ae); cs->arg_len=cs->ae.encodedLength;
    char why[512]; REQUIRE(!nvn_refl_from_json(@{@"route":@"B",@"threadgroup_size":@[@1,@1,@1],@"bindings":@[
        @{@"kind":@"tex_ubo",@"reg":@3,@"off":@0,@"arg_id":@0,@"msl_type":@"texture2d<float>"},
        @{@"kind":@"sbuf",@"reg":@0,@"arg_id":@1,@"rw":@YES}]},&cs->rf,why,sizeof why));
    REQUIRE(cs->rf.nb==2 && cs->rf.b[0].kind==NVN_RB_TEXU);
    NvProg *np=[NvProg new]; np->sh[5]=cs; program=(prog_t){.magic=MAGIC('P','R','O','G'),.serial=nvn_new_serial()}; serial_put(program.serial,np);
    // Warm both texture resolutions and the real compute pipeline before the
    // timed gate. Expected colors are constants, not generated by the backend.
    uint32_t h=RED_ID; memcpy(memory+HANDLE_OFF,&h,4); consume_begin(); finish(consume_end()); REQUIRE(output[0]==1 && output[1]==0 && output[2]==0 && output[3]==1);
    h=BLUE_ID; memcpy(memory+HANDLE_OFF,&h,4); consume_begin(); finish(consume_end()); REQUIRE(output[0]==0 && output[1]==0 && output[2]==1 && output[3]==1);
    for (int i=0;i<4;i++) output[i]=-1;   // B must really write, not return the warm-up's blue output.
    NVNcommandBuffer command; static uint8_t control[4096];
    REQUIRE(API(nvnCommandBufferInitialize)(&command,&device)); API(nvnCommandBufferAddControlMemory)(&command,control,sizeof control);
    API(nvnCommandBufferAddCommandMemory)(&command,&pool,0,CMD); API(nvnCommandBufferBeginRecording)(&command);
    uint32_t red_handle=RED_ID;
    API(nvnCommandBufferUpdateUniformBuffer)(&command,ga+HANDLE_OFF,256,0,4,&red_handle);
    NVNcommandHandle update=API(nvnCommandBufferEndRecording)(&command);
    id<MTLSharedEvent> gate=[g_dev newSharedEvent]; REQUIRE(gate); REQUIRE(gate.signaledValue==0);
    nvn_be_queue_wait_event((__bridge void *)gate,1);
    API(nvnQueueSubmitCommands)(&queue,1,&update); uint64_t a_fence=nvn_be_last_fence();
    REQUIRE(a_fence>0 && !nvn_be_wait(a_fence,0)); REQUIRE(cpu_handle()==BLUE_ID);
    id<MTLSharedEvent> later_gate=nil;
    if (later) {
        // C is a later submitted writer on a different, non-colliding GW page.
        // B's CPU handle decode needs A, not C. C's GPU wait remains closed
        // until B's encoding returns; a six-second fallback prevents deadlock
        // in a broken implementation that waits for the global latest fence.
        REQUIRE(gw_slot(handle_addr>>12)!=gw_slot(nvn_ga2ca(ga+OTHER_OFF)>>12));
        NVNcommandBuffer other; static uint8_t other_control[4096];
        REQUIRE(API(nvnCommandBufferInitialize)(&other,&device)); API(nvnCommandBufferAddControlMemory)(&other,other_control,sizeof other_control);
        API(nvnCommandBufferAddCommandMemory)(&other,&pool,CMD, CMD); API(nvnCommandBufferBeginRecording)(&other);
        API(nvnCommandBufferUpdateUniformBuffer)(&other,ga+OTHER_OFF,256,0,4,&red_handle);
        NVNcommandHandle c=API(nvnCommandBufferEndRecording)(&other);
        later_gate=[g_dev newSharedEvent]; REQUIRE(later_gate);
        nvn_be_queue_wait_event((__bridge void *)later_gate,1); API(nvnQueueSubmitCommands)(&queue,1,&c);
        REQUIRE(nvn_be_last_fence()>a_fence && later_gate.signaledValue==0);
    }
    consume_begin(); REQUIRE(gw_live(handle_addr,handle_addr+4)); REQUIRE(gate.signaledValue==0);
    if (current) mtl_sub_update(&red_handle,handle_addr,4);   // known B-local inline bytes need no A wait
    // An independent CPU timer releases the real GPU event. It must not fire
    // during setup: compiling a shader or a slow setup is SETUP FAIL, not green.
    dispatch_semaphore_t released=dispatch_semaphore_create(0);
    uint64_t start=clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,3*NSEC_PER_SEC),dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        gate.signaledValue=1; dispatch_semaphore_signal(released);
    });
    if (cancel) dispatch_after(dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC),dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{ REQUIRE(!dm_nvn_request_stop()); });
    if (later) dispatch_after(dispatch_time(DISPATCH_TIME_NOW,6*NSEC_PER_SEC),dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{ later_gate.signaledValue=1; });
    REQUIRE(clock_gettime_nsec_np(CLOCK_UPTIME_RAW)-start<100000000ull && gate.signaledValue==0 && cpu_handle()==BLUE_ID);
    uint64_t b_fence=consume_end();
    if (current) CHECK(gate.signaledValue==0,"B-local complete inline handle does not wait for A");
    if (later) { CHECK(later_gate.signaledValue==0,"handle decode waits for A, not later unrelated writer C"); later_gate.signaledValue=1; }
    REQUIRE(dispatch_semaphore_wait(released,dispatch_time(DISPATCH_TIME_NOW,10*NSEC_PER_SEC))==0);
    // Session cancellation makes nvn_be_wait return early by design. Observe
    // the actual production queue signal to verify that B really ran no shader.
    MTLSharedEventListener *listener=[[MTLSharedEventListener alloc] initWithDispatchQueue:dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0)];
    dispatch_semaphore_t completed=dispatch_semaphore_create(0);
    [g_ev notifyListener:listener atValue:b_fence block:^(id<MTLSharedEvent> event, uint64_t value) { (void)event; (void)value; dispatch_semaphore_signal(completed); }];
    REQUIRE(dispatch_semaphore_wait(completed,dispatch_time(DISPATCH_TIME_NOW,10*NSEC_PER_SEC))==0);
    REQUIRE(cpu_handle()==RED_ID);
    if (cancel) {
        CHECK(mtl_last_commit_error() && !mtl_gpu_halted() && output[0]==-1 && output[1]==-1 && output[2]==-1 && output[3]==-1,
            "cancelled CPU handle wait: no shader output (%.1f,%.1f,%.1f,%.1f), failure=%d, GPU-halted=%d",
            output[0],output[1],output[2],output[3],mtl_last_commit_error(),mtl_gpu_halted());
    } else {
    finish(b_fence);
    REQUIRE(output[1]==0 && output[3]==1 && ((output[0]==1 && output[2]==0) || (output[0]==0 && output[2]==1)));
    CHECK(output[0]==1 && output[1]==0 && output[2]==0 && output[3]==1,
        "A GPU UpdateUniformBuffer then B TEXU (no B write): expected red=(1,0,0,1), actual=(%.1f,%.1f,%.1f,%.1f); final CPU handle=%u, A fence=%llu B fence=%llu",
        output[0],output[1],output[2],output[3],cpu_handle(),(unsigned long long)a_fence,(unsigned long long)b_fence);
    }
    printf("%s t_nvn_texu_pending: %u checks, %u failures\n",failures?"✗":"✓",checks,failures); return failures?1:0;
} }
