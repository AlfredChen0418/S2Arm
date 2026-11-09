#!/usr/bin/env python3
"""Production submit/replay/wait bodies with real record_flat; fake queue/Vulkan.

Dirtying only the shim_flat allocation makes missing field initialization
observable. This is a host ownership contract, not a GPU completion proof.
"""
from pathlib import Path
import argparse
import os
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument('--source-ref', help='compile the shim bodies at this git revision')
args = parser.parse_args()
path = 'android/native/nvn/mtl_vk_shim.c'
text = subprocess.check_output(['git', 'show', f'{args.source_ref}:{path}'], cwd=root, text=True) if args.source_ref else (root / path).read_text()

def function(signature):
    start = text.index(signature)
    pos = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[pos] == '{') - (text[pos] == '}')
        pos += 1
    return text[start:pos]

pre = r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stdatomic.h>
#include "nvn_obj.h"
#include "record_flat.h"
struct shim_flat { uint8_t *buf; size_t len; uint64_t fence; };
static uint64_t g_fence, g_submits, g_lost_from_fence;
static int g_sub_active, g_clone_lost;
static _Atomic uint64_t g_replayed_fence, g_completed_fence;
static int poison, enq_fail, deferred, executions;
static void *jobs[8]; static int job_count;
static void *dirty_malloc(size_t n) { void *p = malloc(n); if (p) memset(p, poison, n); return p; }
static void red(const char *format, ...) { (void)format; }
// Empty OP_END carries no guest pointers. These satisfy Mac's link rules and
// fail closed if the fixture accidentally asks to read guest memory.
uint64_t nvn_ga2ca(uint64_t a) { return a; }
int s2a_vk_adapter_guest_read(uint64_t a, void *out, size_t n) { (void)a; (void)out; (void)n; return -1; }
static int exec_chain(const uint8_t *p, int depth) {
    (void)depth; op_hdr h; memcpy(&h, p, sizeof h);
    if (h.op != OP_END || h.size != sizeof h) return -1;
    executions++; return 0;
}
static uint64_t shim_now_ns(void) { return 100; }
static int s2a_vk_adapter_flush_budget(int64_t budget) { (void)budget; return 0; }
'''
job = function('static void job_flat(')
queue = r'''
static int shim_enqueue(void (*fn)(void *), void *p) {
    if (enq_fail) return -1;
    if (deferred) { if (job_count >= 8) return -1; jobs[job_count++] = p; return 0; }
    fn(p); return 0;
}
static void run_one(void) {
    void *p = jobs[0]; memmove(jobs, jobs + 1, (size_t)(--job_count) * sizeof jobs[0]); job_flat(p);
}
static int shim_drain_budget(int64_t budget) { (void)budget; while(job_count) run_one(); return 0; }
'''
main = r'''
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"FAIL line %d: %s (fence=%llu replayed=%llu completed=%llu)\n",__LINE__,#x,(unsigned long long)g_fence,(unsigned long long)atomic_load(&g_replayed_fence),(unsigned long long)atomic_load(&g_completed_fence)); failed++; } } while (0)
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int failed = 0;
    poison = !strcmp(argv[1], "poison") ? 0xA5 : 0;
    g_sub_active = 1;
    uint64_t first = mtl_sub_commit();
    CHECK(first == 1 && executions == 1);
    CHECK(atomic_load(&g_replayed_fence) == first);
    CHECK(mtl_wait(first, 1000000) == 1);
    CHECK(atomic_load(&g_completed_fence) == first);
    // Successful present uses the replayed upper bound as completion proof.
    // It must not claim future work which has not even entered the queue yet.
    shim_note_completed(atomic_load(&g_replayed_fence));
    CHECK(atomic_load(&g_completed_fence) == first);
    deferred = 1; g_sub_active = 1;
    uint64_t second = mtl_sub_commit();
    CHECK(second == 2 && job_count == 1);
    CHECK(mtl_wait(second, 0) == 0);
    CHECK(atomic_load(&g_replayed_fence) == first);
    CHECK(mtl_wait(second, 1000000) == 1);
    CHECK(executions == 2 && atomic_load(&g_replayed_fence) == second);
    enq_fail = 1; g_sub_active = 1;
    uint64_t lost = mtl_sub_commit();
    CHECK(lost == 3 && g_lost_from_fence == lost);
    CHECK(mtl_wait(lost, 1000000) == 0 && atomic_load(&g_completed_fence) == second);
    s2a_record_flat_reset();
    printf("fence %s: replayed=%llu completed=%llu failures=%d\n",argv[1],(unsigned long long)atomic_load(&g_replayed_fence),(unsigned long long)atomic_load(&g_completed_fence),failed);
    return failed ? 1 : 0;
}
'''
source = (pre + '\n' + function('static int clone_append(') + '\n' + job + '\n' + queue + '\n' +
          function('static void shim_note_completed(') + '\n#define malloc dirty_malloc\n' +
          function('uint64_t mtl_sub_commit(') + '\n#undef malloc\n' + function('int mtl_wait(') + '\n' + main)
includes = [root/'android/native/direct', root/'android/native/nvn', root/'android/native',
            root/'graphics/nvn/gfx/nvn', root/'graphics/nvn/gfx/nvn/include', root/'graphics/nvn/gfx/tex']
with tempfile.TemporaryDirectory(prefix='direct-fence-', dir=os.environ.get('S2A_TEST_TMP')) as tmp:
    src=Path(tmp)/'fence.c'; exe=Path(tmp)/'fence'; src.write_text(source)
    cmd=[os.environ.get('CC','cc'),'-std=gnu11','-O1','-Wall','-Wextra','-Werror']
    cmd += ['-I'+str(x) for x in includes]
    cmd += [str(src),str(root/'android/native/direct/record_flat.c'),str(root/'android/native/direct/bind_defer.c'),'-o',str(exe)]
    subprocess.run(cmd,check=True,timeout=90)
    codes=[]
    for mode in ('zero','poison'):
        codes.append(subprocess.run([str(exe),mode],timeout=10).returncode)
    sys.exit(1 if any(codes) else 0)
