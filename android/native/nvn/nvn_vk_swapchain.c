#define VK_USE_PLATFORM_ANDROID_KHR 1
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <android/log.h>
#include <string.h>
#include <sys/system_properties.h>
#include <string.h>
#include <vulkan/vulkan.h>
#include "nvn_vk_swapchain.h"
#include "nvn_vk_surface.h"
#include "nvn_vk_pipeline.h"
#include "nvn_vk_first_draw.h"
#include "nvn_vk_lifecycle.h"
#include "nvn_vk_display_timing.h"

static PFN_vkCreateAndroidSurfaceKHR pCreateSurface;
static PFN_vkGetPhysicalDeviceSurfaceSupportKHR pSupport;
static PFN_vkGetPhysicalDeviceSurfaceCapabilitiesKHR pCaps;
static PFN_vkGetPhysicalDeviceSurfaceFormatsKHR pFormats;
static PFN_vkGetPhysicalDeviceSurfacePresentModesKHR pModes;
static PFN_vkCreateSwapchainKHR pCreateSwapchain;
static PFN_vkDestroySwapchainKHR pDestroySwapchain;
static PFN_vkGetSwapchainImagesKHR pGetImages;
static PFN_vkAcquireNextImageKHR pAcquire;
static PFN_vkQueuePresentKHR pPresent;
static PFN_vkGetPastPresentationTimingGOOGLE pPastTiming;
static PFN_vkGetRefreshCycleDurationGOOGLE pRefreshCycle;

static int load(struct nvn_vk_swapchain *sc) {
    struct s2a_vk_device *d = sc->d;
    pCreateSurface = (PFN_vkCreateAndroidSurfaceKHR)vkGetInstanceProcAddr(d->instance, "vkCreateAndroidSurfaceKHR");
    pSupport = (PFN_vkGetPhysicalDeviceSurfaceSupportKHR)vkGetInstanceProcAddr(d->instance, "vkGetPhysicalDeviceSurfaceSupportKHR");
    pCaps = (PFN_vkGetPhysicalDeviceSurfaceCapabilitiesKHR)vkGetInstanceProcAddr(d->instance, "vkGetPhysicalDeviceSurfaceCapabilitiesKHR");
    pFormats = (PFN_vkGetPhysicalDeviceSurfaceFormatsKHR)vkGetInstanceProcAddr(d->instance, "vkGetPhysicalDeviceSurfaceFormatsKHR");
    pModes = (PFN_vkGetPhysicalDeviceSurfacePresentModesKHR)vkGetInstanceProcAddr(d->instance, "vkGetPhysicalDeviceSurfacePresentModesKHR");
    pCreateSwapchain = (PFN_vkCreateSwapchainKHR)vkGetDeviceProcAddr(d->device, "vkCreateSwapchainKHR");
    pDestroySwapchain = (PFN_vkDestroySwapchainKHR)vkGetDeviceProcAddr(d->device, "vkDestroySwapchainKHR");
    pGetImages = (PFN_vkGetSwapchainImagesKHR)vkGetDeviceProcAddr(d->device, "vkGetSwapchainImagesKHR");
    pAcquire = (PFN_vkAcquireNextImageKHR)vkGetDeviceProcAddr(d->device, "vkAcquireNextImageKHR");
    pPresent = (PFN_vkQueuePresentKHR)vkGetDeviceProcAddr(d->device, "vkQueuePresentKHR");
    // B2：display_timing 入口只在设备端已启用扩展时取（默认关，拿不到时 nvn_vk_dt_attach 自己报 unsupported）。
    if (d->has_display_timing) {
        pPastTiming = (PFN_vkGetPastPresentationTimingGOOGLE)vkGetDeviceProcAddr(d->device, "vkGetPastPresentationTimingGOOGLE");
        pRefreshCycle = (PFN_vkGetRefreshCycleDurationGOOGLE)vkGetDeviceProcAddr(d->device, "vkGetRefreshCycleDurationGOOGLE");
    } else {
        pPastTiming = NULL;
        pRefreshCycle = NULL;
    }
    return pCreateSurface && pSupport && pCaps && pFormats && pModes && pCreateSwapchain && pDestroySwapchain && pGetImages && pAcquire && pPresent;
}

static int64_t fd_now_ns_sc(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000ll + ts.tv_nsec;
}

static int fail(char *why, size_t n, const char *text) { if (why && n) snprintf(why, n, "%s", text); return -1; }

// present 记账：同一时刻只有一个 swapchain（单窗口 owner），这里保留最近一次的快照供诊断/验收读取。
static struct nvn_vk_swapchain *g_present_sc;
void nvn_vk_swapchain_present_stats(struct nvn_vk_swapchain_present_stats *out) {
    if (!out) return;
    memset(out, 0, sizeof *out);
    if (!g_present_sc) return;
    out->submitted = g_present_sc->submitted;
    out->completed = g_present_sc->completed;
    out->retired = g_present_sc->retired;
    out->submit_failed = g_present_sc->submit_failed;
    out->present_failed = g_present_sc->present_failed_count;
    out->fence_timeout = g_present_sc->fence_timeout;
    out->retire_timeout = g_present_sc->retire_timeout;
    out->acquire_timeout = g_present_sc->acquire_timeout;
    out->retained = g_present_sc->nretained;
    out->pending = g_present_sc->npresent_pending;
    out->present_failed_flag = g_present_sc->present_failed;
    out->retire_by_reacquire = g_present_sc->retire_by_reacquire;
    out->idle_assumed = g_present_sc->idle_assumed;
}

// W6.0/W6.1：低频呈现账。四态分开报：present 已提交、渲染 fence 完成（同步路径）、
// present fence 退休、仍在途/保留；失败计数不折叠。GPU 时间取最近一次已完成的帧，
// 没取到就写 unavailable，不写 0 ms。每 64 次成功 present 一行，采集器离线读。
static void present_account_tick(struct nvn_vk_swapchain *sc) {
    static uint64_t n;
    if (!sc || ++n % 64 != 0) return;
    double gpu_ms = 0.0;
    int passes = 0;
    uint64_t gpu_frame = 0;
    int gpu_ok = nvn_vk_first_draw_gpu_last(&gpu_ms, &passes, &gpu_frame);
    __android_log_print(ANDROID_LOG_INFO, "S2Arm",
        "present account: submitted=%llu draw_completed_sync=%llu presentation_retired=%llu "
        "pending=%d retained=%d deferred_retired=%llu | "
        "fail(submit=%llu present=%llu fence_timeout=%llu retire_timeout=%llu acquire_timeout=%llu) | "
        "gpu=%s dt=%s",
        (unsigned long long)sc->submitted, (unsigned long long)sc->completed,
        (unsigned long long)sc->retired, sc->npresent_pending, sc->nretained,
        (unsigned long long)nvn_vk_first_draw_deferred_retired(),
        (unsigned long long)sc->submit_failed, (unsigned long long)sc->present_failed_count,
        (unsigned long long)sc->fence_timeout, (unsigned long long)sc->retire_timeout,
        (unsigned long long)sc->acquire_timeout,
        gpu_ok ? "valid" : "unavailable", nvn_vk_dt_state());
    // B2：计量中时报真实上屏间隔（actualPresentTime 相邻帧差），QA 直接量真实帧率。
    if (nvn_vk_dt_active()) {
        uint64_t tg = 0, rv = 0, pd = 0; double p50 = 0, p95 = 0, mx = 0, rf = 0;
        nvn_vk_dt_stats(&tg, &rv, &pd, &p50, &p95, &mx, &rf);
        __android_log_print(ANDROID_LOG_INFO, "S2Arm",
            "display timing: tagged=%llu received=%llu pending=%llu | shown gap p50=%.2fms p95=%.2fms max=%.2fms | refresh=%.3fms",
            (unsigned long long)tg, (unsigned long long)rv, (unsigned long long)pd, p50, p95, mx, rf / 1e6);
    }
    if (gpu_ok)
        __android_log_print(ANDROID_LOG_INFO, "S2Arm",
            "present account gpu: last=%.2f ms frame=%llu passes=%d (read after completion)",
            gpu_ms, (unsigned long long)gpu_frame, passes);
}

// 诊断注入（与 debug.s2a.fail_submit 同一类，只用于验收失败/退休路径）：
// 1 = 提交成功后假装 present 报错；2 = present 成功后假装等不到 fence。0 = 正常语义。
static int present_fault_mode(void) {
    char v[PROP_VALUE_MAX] = {0};
    __system_property_get("debug.s2a.fail_present", v);
    return v[0] ? atoi(v) : 0;
}

// 已提交但没有完成/呈现证据：命令缓冲和 fence 保留（Vulkan 禁止释放/复用），present_done 不调用。
static void present_retain(struct nvn_vk_swapchain *sc, VkFence fence, VkFence present_fence, VkCommandBuffer cmd) {
    if (sc->nretained >= NVK_SC_RETAINED) {
        __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                            "present: retained-command table full (%d); in-flight present command kept but not tracked",
                            NVK_SC_RETAINED);
        return;
    }
    sc->retained[sc->nretained].fence = fence;
    sc->retained[sc->nretained].present_fence = present_fence;
    sc->retained[sc->nretained].cmd = cmd;
    sc->nretained++;
}

// pending 表的增删：present fence、图像序号、再 acquire 标记三列同步移动。
static void pending_remove(struct nvn_vk_swapchain *sc, int i) {
    for (int k = i + 1; k < sc->npresent_pending; k++) {
        sc->present_pending[k - 1] = sc->present_pending[k];
        sc->present_image[k - 1] = sc->present_image[k];
        sc->present_reacquired[k - 1] = sc->present_reacquired[k];
    }
    sc->npresent_pending--;
}
static void pending_push(struct nvn_vk_swapchain *sc, VkFence present_fence, int image) {
    sc->present_pending[sc->npresent_pending] = present_fence;
    sc->present_image[sc->npresent_pending] = (int8_t)image;
    sc->present_reacquired[sc->npresent_pending] = 0;
    sc->npresent_pending++;
}

// #426 再 acquire 证明：acquire 成功交回第 index 张图时，把这张图此前的呈现标成“已被再次交回”；
// 那次 acquire 的 fence 触发后（reacquire_observe）才算退休。本帧随后的 present 在标记之后入表，
// 不会被这次 acquire 错误地带走。
static void reacquire_mark(struct nvn_vk_swapchain *sc, uint32_t index) {
    sc->acquire_fence_armed = 1;
    for (int i = 0; i < sc->npresent_pending; i++)
        if (sc->present_image[i] == (int)index) sc->present_reacquired[i] = 1;
}

// 观察已交给 acquire 的 fence。budget_ns==0 纯轮询，>0 有界等待。
// 0 = 未挂起或已触发（标记过的呈现记退休、fence 复位可再用）；1 = 仍未触发；-1 = fence 状态不可用。
static int reacquire_observe(struct nvn_vk_swapchain *sc, int64_t budget_ns) {
    if (!sc->acquire_fence_armed) return 0;
    VkResult wr = budget_ns == 0 ? vkGetFenceStatus(sc->d->device, sc->acquire_fence)
                                 : vkWaitForFences(sc->d->device, 1, &sc->acquire_fence, VK_TRUE,
                                                   budget_ns < 0 ? UINT64_MAX : (uint64_t)budget_ns);
    if (wr == VK_TIMEOUT || wr == VK_NOT_READY) return 1;
    if (wr != VK_SUCCESS) {
        __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                            "present retire: acquire fence status=%d; reacquire retirement proof unavailable "
                            "(%d present(s) kept unretired)", (int)wr, sc->npresent_pending);
        return -1;
    }
    if (vkResetFences(sc->d->device, 1, &sc->acquire_fence) != VK_SUCCESS) return -1;
    sc->acquire_fence_armed = 0;
    int i = 0;
    while (i < sc->npresent_pending) {
        if (sc->present_reacquired[i]) {
            pending_remove(sc, i);
            sc->retired++;
            continue;
        }
        i++;
    }
    return 0;
}

// 三条呈现路径共用的 acquire。present fence 模式与原来一样不带 fence；再 acquire 模式先确认上一次
// acquire 的 fence 已触发（触发前不能复用），再带着它 acquire，成功后标记这张图此前的呈现。
static VkResult acquire_image(struct nvn_vk_swapchain *sc, uint32_t *index) {
    if (!sc->retire_by_reacquire)
        return pAcquire(sc->d->device, sc->swapchain, (uint64_t)NVK_PRESENT_ACQUIRE_TIMEOUT_NS,
                        sc->acquire, VK_NULL_HANDLE, index);
    int orr = reacquire_observe(sc, NVK_PRESENT_RETIRE_BUDGET_NS);
    if (orr != 0) {
        if (orr > 0) sc->retire_timeout++;
        __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                            "present: previous acquire fence %s; cannot reuse it for the next acquire",
                            orr > 0 ? "did not signal within the retirement budget" : "status is unusable");
        return VK_ERROR_UNKNOWN;
    }
    VkResult ar = pAcquire(sc->d->device, sc->swapchain, (uint64_t)NVK_PRESENT_ACQUIRE_TIMEOUT_NS,
                           sc->acquire, sc->acquire_fence, index);
    // 规范：acquire 没交回图像（TIMEOUT/NOT_READY/错误）时不动 fence/semaphore，fence 保持可用。
    if ((ar == VK_SUCCESS || ar == VK_SUBOPTIMAL_KHR) && *index < sc->image_count) reacquire_mark(sc, *index);
    else if (ar == VK_SUCCESS || ar == VK_SUBOPTIMAL_KHR) sc->acquire_fence_armed = 1;   // 越界序号：fence 照样会触发
    (void)reacquire_observe(sc, 0);   // 多数时候图像早已空出：当场收账
    return ar;
}

// 呈现退休：只对已经交给呈现引擎的 present fence 做观察。budget_ns==0 是纯轮询（不等待）；
// >0 是有界等待；<0 无限等（只给进程收尾的显式路径用，不作为生产帧路径的默认）。
// 返回 0 = 队列已清空；1 = 仍有未 signal 的 present fence（不是失败）；-1 = fence 状态不可用。
int nvn_vk_swapchain_retire_present(struct nvn_vk_swapchain *sc, int64_t budget_ns) {
    if (!sc || !sc->d) return 0;
    if (sc->retire_by_reacquire && sc->acquire_fence_armed && sc->npresent_pending > 0 &&
        reacquire_observe(sc, budget_ns) < 0)
        return -1;
    if (sc->npresent_pending <= 0) return 0;
    const int64_t deadline = budget_ns < 0 ? -1 : fd_now_ns_sc() + budget_ns;
    int i = 0;
    while (i < sc->npresent_pending) {
        VkFence fence = sc->present_pending[i];
        VkResult wr;
        if (fence == VK_NULL_HANDLE) {
            // 再 acquire 模式的条目（只能等同图像再 acquire），或诊断注入（debug.s2a.fail_present=2）
            // 的占位：没有可观察的 present fence，不伪造 signal，也不释放。按“仍在途”保留。
            i++;
            continue;
        }
        if (budget_ns == 0) {
            wr = vkGetFenceStatus(sc->d->device, fence);
        } else {
            int64_t left = deadline < 0 ? (int64_t)UINT64_MAX : deadline - fd_now_ns_sc();
            if (left <= 0 && budget_ns > 0) wr = VK_TIMEOUT;
            else wr = vkWaitForFences(sc->d->device, 1, &fence, VK_TRUE, (uint64_t)left);
        }
        if (wr == VK_SUCCESS) {
            vkDestroyFence(sc->d->device, fence, NULL);
            pending_remove(sc, i);
            sc->retired++;
            continue;
        }
        if (wr == VK_TIMEOUT || wr == VK_NOT_READY) { i++; continue; }
        __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                            "present retire: fence status=%d; presentation retirement proof unavailable "
                            "(%d present(s) kept unretired)", (int)wr, sc->npresent_pending);
        return -1;
    }
    return sc->npresent_pending ? 1 : 0;
}

// 失败终态：present 路径不能再用这份 swapchain（信号量状态已不保证），但对象不被销毁。
static int present_fail_state(struct nvn_vk_swapchain *sc, char *why, size_t why_size, const char *text) {
    if (!sc->present_failed) {
        sc->present_failed = 1;
        snprintf(sc->present_fail, sizeof sc->present_fail, "%s", text ? text : "present path failed");
        __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                            "present: %s; swapchain present path retired (window retirement required, "
                            "%d in-flight command set(s) retained)", text ? text : "failed", sc->nretained);
    }
    return fail(why, why_size, text);
}

// 再 acquire 模式：present 已交给呈现引擎，记一条按图像序号等再 acquire 的 pending。每次 acquire 前
// 都先观察上一次 acquire 的 fence（acquire_image），同一张图最多留一条，表长 ≥ 图像数，正常不会满；
// 满了不能丢条目，进入失败终态（窗口退休时由队列空闲假设收口，见 destroy）。
static int reacquire_track(struct nvn_vk_swapchain *sc, uint32_t index, char *why, size_t why_size) {
    if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING) (void)nvn_vk_swapchain_retire_present(sc, 0);
    if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING) {
        present_retain(sc, VK_NULL_HANDLE, VK_NULL_HANDLE, VK_NULL_HANDLE);
        sc->retire_timeout++;
        return present_fail_state(sc, why, why_size,
                                  "reacquire retirement tracking table is full; presentation retirement not claimed");
    }
    pending_push(sc, VK_NULL_HANDLE, (int)index);
    return 0;
}

int nvn_vk_swapchain_create(struct nvn_vk_swapchain *sc, struct s2a_vk_device *d, ANativeWindow *window, char *why, size_t why_size) {
    memset(sc, 0, sizeof *sc);
    sc->d = d;
    if (!d || !window) return fail(why, why_size, "null device/window");
    sc->window = window;
    // B3 输出尺寸出图：开关在建链时读一次；会话中途改开关只影响下一次窗口 attach。
    char bg[PROP_VALUE_MAX] = {0};
    __system_property_get("debug.s2a.buffer_geometry", bg);
    sc->buf_geometry = bg[0] == '1';
    // 直连合同：present 必须有真实呈现退休证据。有 VK_EXT_swapchain_maintenance1 用它的 present fence；
    // 没有（安卓 13 及以下的系统 libvulkan 不补这个扩展，#426）就用同图像再 acquire：同一序号的图像
    // 再次被 acquire 且那次 acquire 的 fence 已触发，才证明这张图上一次的呈现已结束。
    // 渲染 fence 只证明“画完”，两种模式都不拿它冒充呈现退休。
    sc->retire_by_reacquire = !d->swapchain_maintenance1_enabled;
    if (!load(sc)) return fail(why, why_size, "WSI entry points missing");
    VkAndroidSurfaceCreateInfoKHR si = { VK_STRUCTURE_TYPE_ANDROID_SURFACE_CREATE_INFO_KHR };
    si.window = window;
    if (pCreateSurface(d->instance, &si, NULL, &sc->surface) != VK_SUCCESS) return fail(why, why_size, "vkCreateAndroidSurfaceKHR failed");
    VkBool32 supported = VK_FALSE;
    pSupport(d->physical, d->queue_family, sc->surface, &supported);
    if (!supported) return fail(why, why_size, "queue family cannot present this surface");
    VkSurfaceCapabilitiesKHR caps;
    pCaps(d->physical, sc->surface, &caps);
    uint32_t fc = 0, mc = 0;
    pFormats(d->physical, sc->surface, &fc, NULL);
    VkSurfaceFormatKHR fmts[32]; if (fc > 32) fc = 32;
    pFormats(d->physical, sc->surface, &fc, fmts);
    pModes(d->physical, sc->surface, &mc, NULL);
    VkPresentModeKHR modes[16]; if (mc > 16) mc = 16;
    pModes(d->physical, sc->surface, &mc, modes);
    VkSurfaceFormatKHR chosen = nvn_vk_pick_surface_format(fmts, fc, VK_FORMAT_R8G8B8A8_UNORM, VK_COLOR_SPACE_SRGB_NONLINEAR_KHR);
    if (chosen.format == VK_FORMAT_UNDEFINED) return fail(why, why_size, "surface has no usable format");
    if (chosen.format == VK_FORMAT_R8G8B8A8_SRGB || chosen.format == VK_FORMAT_B8G8R8A8_SRGB)
        __android_log_print(ANDROID_LOG_WARN, "S2Arm",
            "swapchain: surface offers no UNORM format; SRGB present may double-encode the UNORM game render target");

    VkPresentModeKHR pm = nvn_vk_pick_present_mode(modes, mc, VK_PRESENT_MODE_FIFO_KHR);
    uint32_t ww = (uint32_t)ANativeWindow_getWidth(window), wh = (uint32_t)ANativeWindow_getHeight(window);
    // 诊断：源图/窗口/swapchain 各自的坐标系与尺寸。
    __android_log_print(ANDROID_LOG_INFO, "S2Arm",
        "swapchain caps: window=%ux%u currentExtent=%ux%u min=%ux%u max=%ux%u currentTransform=0x%x supportedTransforms=0x%x",
        ww, wh, caps.currentExtent.width, caps.currentExtent.height,
        caps.minImageExtent.width, caps.minImageExtent.height,
        caps.maxImageExtent.width, caps.maxImageExtent.height,
        (unsigned)caps.currentTransform, (unsigned)caps.supportedTransforms);
    // Android 契约（graphics/vulkan/wsi/AndroidWindow.cs）：surface 支持 Identity 时把旋转交给
    // Android 合成器，swapchain 用发布窗口坐标；不能把 currentTransform 当 preTransform 又不转内容，
    // 那会让合成器再转一次（实测 2712x1220 窗口、ROTATE_90、源图横排 → 屏幕竖排）。
    if (!(caps.supportedTransforms & VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR))
        return fail(why, why_size, "surface does not support identity preTransform; pre-rotation path not implemented");
    if (!ww || !wh) {
        if (caps.currentExtent.width == UINT32_MAX || !caps.currentExtent.width || !caps.currentExtent.height)
            return fail(why, why_size, "window and surface extent are both unavailable");
        ww = caps.currentExtent.width; wh = caps.currentExtent.height;
    }
    uint32_t w = ww, h = wh;
    if (w < caps.minImageExtent.width) w = caps.minImageExtent.width;
    if (w > caps.maxImageExtent.width) w = caps.maxImageExtent.width;
    if (h < caps.minImageExtent.height) h = caps.minImageExtent.height;
    if (h > caps.maxImageExtent.height) h = caps.maxImageExtent.height;
    if (!w || !h) return fail(why, why_size, "clamped swapchain extent is zero");
    VkCompositeAlphaFlagBitsKHR alpha = (caps.supportedCompositeAlpha & VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR) ? VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR : VK_COMPOSITE_ALPHA_INHERIT_BIT_KHR;
    VkSwapchainCreateInfoKHR ci = { VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR };
    ci.surface = sc->surface; ci.minImageCount = caps.minImageCount + 1;
    ci.imageFormat = chosen.format; ci.imageColorSpace = chosen.colorSpace; ci.imageExtent = (VkExtent2D){w, h};
    ci.imageArrayLayers = 1; ci.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    ci.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE; ci.preTransform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR;
    __android_log_print(ANDROID_LOG_INFO, "S2Arm",
        "swapchain create: extent=%ux%u preTransform=0x%x fmt=%d colorspace=%d presentMode=%d presented-retirement=%s visible-proof=none",
        w, h, (unsigned)ci.preTransform, (int)chosen.format, (int)chosen.colorSpace, (int)pm,
        sc->retire_by_reacquire ? "same-image-reacquire(no maintenance1)" : "present-fence(EXT)");
    ci.compositeAlpha = alpha; ci.presentMode = pm; ci.clipped = VK_TRUE;
    if (pCreateSwapchain(d->device, &ci, NULL, &sc->swapchain) != VK_SUCCESS) return fail(why, why_size, "vkCreateSwapchainKHR failed");
    pGetImages(d->device, sc->swapchain, &sc->image_count, NULL);
    if (sc->image_count > 8) sc->image_count = 8;
    pGetImages(d->device, sc->swapchain, &sc->image_count, sc->images);
    VkSemaphoreCreateInfo sem = { VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO };
    VkFenceCreateInfo fci = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
    if (vkCreateSemaphore(d->device, &sem, NULL, &sc->acquire) != VK_SUCCESS)
        return fail(why, why_size, "acquire semaphore create failed");
    for (uint32_t i = 0; i < sc->image_count; i++)
        if (vkCreateSemaphore(d->device, &sem, NULL, &sc->release[i]) != VK_SUCCESS)
            return fail(why, why_size, "per-image present semaphore create failed");
    if (vkCreateFence(d->device, &fci, NULL, &sc->fence) != VK_SUCCESS)
        return fail(why, why_size, "draw-completion fence create failed");
    if (sc->retire_by_reacquire && vkCreateFence(d->device, &fci, NULL, &sc->acquire_fence) != VK_SUCCESS)
        return fail(why, why_size, "reacquire-proof acquire fence create failed");
    sc->format = chosen.format; sc->extent = (VkExtent2D){w, h};
    sc->window_width = ww ? ww : w;
    sc->window_height = wh ? wh : h;
    // B3：此时缓冲还没被改成游戏输出尺寸，getWidth/getHeight 仍是 view 的像素尺寸；
    // 之后 buffer_geometry 重建会让这两个 API 报告缓冲尺寸，窗口像素只能用这里记下的值。
    sc->frame_width = sc->window_width;
    sc->frame_height = sc->window_height;
    // B2：swapchain 就绪即绑定 display_timing；不支持/被关时 attach 内部报原因，present 路径零改动。
    nvn_vk_dt_attach(d->device, d->has_display_timing, pPastTiming, pRefreshCycle);
    sc->ready = 1;
    return 0;
}

int nvn_vk_swapchain_clear_present(struct nvn_vk_swapchain *sc, float r, float g, float b, float a, char *why, size_t why_size) {
    if (sc && sc->present_failed)
        return fail(why, why_size, "clear present: swapchain present path already failed (window retirement required)");
    if (!sc || !sc->ready) return fail(why, why_size, "swapchain not ready");
    if (sc->acquire_pending)
        return fail(why, why_size, "clear present: previous acquire has no consuming submit; swapchain unusable");
    g_present_sc = sc;
    uint32_t index = 0;
    VkResult ar = acquire_image(sc, &index);
    if (ar == VK_TIMEOUT || ar == VK_NOT_READY)
        return fail(why, why_size, "clear present: acquire timed out; frame skipped (no infinite wait)");
    if (ar != VK_SUCCESS && ar != VK_SUBOPTIMAL_KHR) return fail(why, why_size, "vkAcquireNextImageKHR failed");
    if (index >= sc->image_count) return fail(why, why_size, "swapchain index out of range");
    sc->acquire_pending = 1;   // 提交成功前不能销毁/复用 acquire 信号量
    vkResetFences(sc->d->device, 1, &sc->fence);
    VkCommandBufferAllocateInfo cai = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO };
    cai.commandPool = sc->d->command_pool; cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cai.commandBufferCount = 1;
    VkCommandBuffer cmd;
    if (vkAllocateCommandBuffers(sc->d->device, &cai, &cmd) != VK_SUCCESS)
        return present_fail_state(sc, why, why_size, "command buffer alloc failed after acquire; acquire state kept");
    VkCommandBufferBeginInfo bi = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vkBeginCommandBuffer(cmd, &bi);
    VkImageMemoryBarrier to_dst = { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER };
    to_dst.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED; to_dst.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    to_dst.srcQueueFamilyIndex = to_dst.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    to_dst.image = sc->images[index];
    to_dst.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    to_dst.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &to_dst);
    VkClearColorValue clear = { { r, g, b, a } };
    VkImageSubresourceRange range = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 };
    vkCmdClearColorImage(cmd, sc->images[index], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &clear, 1, &range);
    VkImageMemoryBarrier to_present = to_dst;
    to_present.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL; to_present.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    to_present.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT; to_present.dstAccessMask = 0;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, NULL, 0, NULL, 1, &to_present);
    vkEndCommandBuffer(cmd);
    VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT;
    VkSubmitInfo submit = { VK_STRUCTURE_TYPE_SUBMIT_INFO };
    submit.waitSemaphoreCount = 1; submit.pWaitSemaphores = &sc->acquire; submit.pWaitDstStageMask = &wait_stage;
    submit.commandBufferCount = 1; submit.pCommandBuffers = &cmd;
    submit.signalSemaphoreCount = 1; submit.pSignalSemaphores = &sc->release[index];
    // 探针路径也必须区分“画完”与“呈现退休”：同步等 present fence（探针不追帧节拍）。
    // 再 acquire 模式没有 present fence：画完后按图像序号记 pending，等同图像再 acquire。
    VkFenceCreateInfo pfci = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
    VkFence present_fence = VK_NULL_HANDLE;
    if (!sc->retire_by_reacquire && vkCreateFence(sc->d->device, &pfci, NULL, &present_fence) != VK_SUCCESS) {
        vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
        return present_fail_state(sc, why, why_size,
                                  "present-retirement fence create failed after acquire; acquire state kept");
    }
    VkSwapchainPresentFenceInfoEXT fence_info = { VK_STRUCTURE_TYPE_SWAPCHAIN_PRESENT_FENCE_INFO_EXT };
    fence_info.swapchainCount = 1; fence_info.pFences = &present_fence;
    nvn_vk_callring_add("present submit cmd=%p fence=%p", (void *)cmd, (void *)sc->fence);
    if (vkQueueSubmit(sc->d->queue, 1, &submit, sc->fence) != VK_SUCCESS) {
        if (present_fence) vkDestroyFence(sc->d->device, present_fence, NULL);
        return present_fail_state(sc, why, why_size,
                                  "vkQueueSubmit failed; acquired image/command kept, acquire state unproven");
    }
    sc->acquire_pending = 0;   // 提交已 wait acquire 信号量
    VkPresentInfoKHR pi = { VK_STRUCTURE_TYPE_PRESENT_INFO_KHR };
    if (present_fence) pi.pNext = &fence_info;
    pi.waitSemaphoreCount = 1; pi.pWaitSemaphores = &sc->release[index];
    pi.swapchainCount = 1; pi.pSwapchains = &sc->swapchain; pi.pImageIndices = &index;
    VkResult pr = pPresent(sc->d->queue, &pi);
    if (pr != VK_SUCCESS && pr != VK_SUBOPTIMAL_KHR) {
        present_retain(sc, sc->fence, present_fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        return present_fail_state(sc, why, why_size, "vkQueuePresentKHR failed after submit; fences/command retained");
    }
    int drew_ok = vkWaitForFences(sc->d->device, 1, &sc->fence, VK_TRUE, 2000000000ull) == VK_SUCCESS;
    if (sc->retire_by_reacquire) {
        if (!drew_ok) {
            present_retain(sc, sc->fence, VK_NULL_HANDLE, cmd);
            sc->fence = VK_NULL_HANDLE;
            sc->fence_timeout++;
            return present_fail_state(sc, why, why_size, "clear-present draw completion not proven; command/fence retained");
        }
        vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
        sc->completed++;
        return reacquire_track(sc, index, why, why_size);
    }
    int retired_ok = drew_ok &&
        vkWaitForFences(sc->d->device, 1, &present_fence, VK_TRUE, (uint64_t)NVK_PRESENT_RETIRE_BUDGET_NS) == VK_SUCCESS;
    if (!retired_ok) {
        present_retain(sc, sc->fence, present_fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        if (!drew_ok) sc->fence_timeout++; else sc->retire_timeout++;
        return present_fail_state(sc, why, why_size, "clear-present retirement not proven; command/fences retained");
    }
    vkDestroyFence(sc->d->device, present_fence, NULL);
    vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
    sc->completed++;
    sc->retired++;
    return 0;
}

int nvn_vk_swapchain_draw_present(struct nvn_vk_swapchain *sc, struct nvn_vk_pipeline_cache *pc, VkPipeline pipe,
                                  const void *verts, size_t vert_bytes, VkBuffer ubo, VkDeviceSize ubo_size,
                                  char *why, size_t why_size) {
    if (sc && sc->present_failed)
        return fail(why, why_size, "draw present: swapchain present path already failed (window retirement required)");
    if (!sc || !sc->ready || !pc || !pipe || !verts || vert_bytes < 16) return fail(why, why_size, "draw present: bad args");
    if (sc->acquire_pending)
        return fail(why, why_size, "draw present: previous acquire has no consuming submit; swapchain unusable");
    g_present_sc = sc;
    uint32_t index = 0;
    VkResult ar = acquire_image(sc, &index);
    if (ar == VK_TIMEOUT || ar == VK_NOT_READY)
        return fail(why, why_size, "draw present: acquire timed out; frame skipped (no infinite wait)");
    if (ar != VK_SUCCESS && ar != VK_SUBOPTIMAL_KHR) return fail(why, why_size, "vkAcquireNextImageKHR failed");
    if (index >= sc->image_count) return fail(why, why_size, "swapchain index out of range");
    sc->acquire_pending = 1;   // 提交成功前不能销毁/复用 acquire 信号量
    VkImageViewCreateInfo vci = { VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO };
    vci.image = sc->images[index]; vci.viewType = VK_IMAGE_VIEW_TYPE_2D; vci.format = sc->format;
    vci.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    VkImageView view = VK_NULL_HANDLE;
    if (vkCreateImageView(sc->d->device, &vci, NULL, &view) != VK_SUCCESS)
        return present_fail_state(sc, why, why_size, "swapchain view create failed after acquire; acquire state kept");
    VkCommandBufferAllocateInfo cai = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO };
    cai.commandPool = sc->d->command_pool; cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cai.commandBufferCount = 1;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    if (vkAllocateCommandBuffers(sc->d->device, &cai, &cmd) != VK_SUCCESS) {
        vkDestroyImageView(sc->d->device, view, NULL);
        return present_fail_state(sc, why, why_size, "command buffer alloc failed after acquire; acquire state kept");
    }
    VkCommandBufferBeginInfo bi = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vkBeginCommandBuffer(cmd, &bi);
    VkImageMemoryBarrier to_color = { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER };
    to_color.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED; to_color.newLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    to_color.srcQueueFamilyIndex = to_color.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    to_color.image = sc->images[index];
    to_color.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    to_color.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, 0, 0, NULL, 0, NULL, 1, &to_color);
    VkBufferCreateInfo bci = { VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO };
    bci.size = vert_bytes; bci.usage = VK_BUFFER_USAGE_VERTEX_BUFFER_BIT;
    VkBuffer vb = VK_NULL_HANDLE; vkCreateBuffer(sc->d->device, &bci, NULL, &vb);
    VkMemoryRequirements vmr; vkGetBufferMemoryRequirements(sc->d->device, vb, &vmr);
    VkMemoryAllocateInfo vai = { VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO };
    vai.allocationSize = vmr.size;
    vai.memoryTypeIndex = s2a_vk_memory_type(sc->d, vmr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, NULL);
    VkDeviceMemory vmem = VK_NULL_HANDLE; vkAllocateMemory(sc->d->device, &vai, NULL, &vmem); vkBindBufferMemory(sc->d->device, vb, vmem, 0);
    void *vp = NULL; if (vkMapMemory(sc->d->device, vmem, 0, vert_bytes, 0, &vp) == VK_SUCCESS) { memcpy(vp, verts, vert_bytes); vkUnmapMemory(sc->d->device, vmem); }
    VkFramebuffer fb = VK_NULL_HANDLE;
    int rc = nvn_vk_pipeline_record(pc, pipe, cmd, view, sc->extent.width, sc->extent.height, vb, ubo, ubo_size, &fb);
    VkImageMemoryBarrier to_present = to_color;
    to_present.oldLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL; to_present.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    to_present.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT; to_present.dstAccessMask = 0;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, NULL, 0, NULL, 1, &to_present);
    vkEndCommandBuffer(cmd);
    VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    VkSubmitInfo si = { VK_STRUCTURE_TYPE_SUBMIT_INFO };
    si.waitSemaphoreCount = 1; si.pWaitSemaphores = &sc->acquire; si.pWaitDstStageMask = &wait_stage;
    si.commandBufferCount = 1; si.pCommandBuffers = &cmd;
    si.signalSemaphoreCount = 1; si.pSignalSemaphores = &sc->release[index];
    vkResetFences(sc->d->device, 1, &sc->fence);
    VkFenceCreateInfo pfci = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
    VkFence present_fence = VK_NULL_HANDLE;
    if (!sc->retire_by_reacquire && vkCreateFence(sc->d->device, &pfci, NULL, &present_fence) != VK_SUCCESS) {
        if (fb) vkDestroyFramebuffer(sc->d->device, fb, NULL);
        vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
        vkDestroyImageView(sc->d->device, view, NULL);
        vkDestroyBuffer(sc->d->device, vb, NULL); vkFreeMemory(sc->d->device, vmem, NULL);
        return present_fail_state(sc, why, why_size,
                                  "retirement fence create failed after acquire; acquire state kept");
    }
    if (rc != 0 || vkQueueSubmit(sc->d->queue, 1, &si, sc->fence) != VK_SUCCESS) {
        // 命令从未提交：命令缓冲与它的 framebuffer/view/buffer 全部可回收。
        if (present_fence) vkDestroyFence(sc->d->device, present_fence, NULL);
        if (fb) vkDestroyFramebuffer(sc->d->device, fb, NULL);
        vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
        vkDestroyImageView(sc->d->device, view, NULL);
        vkDestroyBuffer(sc->d->device, vb, NULL); vkFreeMemory(sc->d->device, vmem, NULL);
        return present_fail_state(sc, why, why_size,
                                  "draw present: submit failed; acquired image/command state kept");
    }
    sc->acquire_pending = 0;
    VkSwapchainPresentFenceInfoEXT fence_info = { VK_STRUCTURE_TYPE_SWAPCHAIN_PRESENT_FENCE_INFO_EXT };
    fence_info.swapchainCount = 1; fence_info.pFences = &present_fence;
    VkPresentInfoKHR pi = { VK_STRUCTURE_TYPE_PRESENT_INFO_KHR };
    if (present_fence) pi.pNext = &fence_info;
    pi.waitSemaphoreCount = 1; pi.pWaitSemaphores = &sc->release[index];
    pi.swapchainCount = 1; pi.pSwapchains = &sc->swapchain; pi.pImageIndices = &index;
    VkResult pr = pPresent(sc->d->queue, &pi);
    if (pr != VK_SUCCESS && pr != VK_SUBOPTIMAL_KHR) {
        present_retain(sc, sc->fence, present_fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        // retained command 仍引用 fb/view/vb/vmem：不销毁它们（随设备/进程收尾）。
        sc->present_failed_count++;
        return present_fail_state(sc, why, why_size,
                                  "draw present: present failed after submit; command/fences retained");
    }
    int drew_ok = vkWaitForFences(sc->d->device, 1, &sc->fence, VK_TRUE, 2000000000ull) == VK_SUCCESS;
    // 再 acquire 模式：present 只读交换链图像，画完即可回收命令与 fb/view/vb；呈现退休另记 pending。
    int retired_ok = drew_ok && (sc->retire_by_reacquire ||
        vkWaitForFences(sc->d->device, 1, &present_fence, VK_TRUE, (uint64_t)NVK_PRESENT_RETIRE_BUDGET_NS) == VK_SUCCESS);
    if (!retired_ok) {
        present_retain(sc, sc->fence, present_fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        if (!drew_ok) sc->fence_timeout++; else sc->retire_timeout++;
        // 同 present 失败：命令引用的 fb/view/vb/vmem 保留，不在这里销毁。
        return present_fail_state(sc, why, why_size,
                                  "draw present: completion/retirement not proven; command/fences retained");
    }
    if (present_fence) vkDestroyFence(sc->d->device, present_fence, NULL);
    if (fb) vkDestroyFramebuffer(sc->d->device, fb, NULL);
    vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
    vkDestroyImageView(sc->d->device, view, NULL);
    vkDestroyBuffer(sc->d->device, vb, NULL); vkFreeMemory(sc->d->device, vmem, NULL);
    sc->completed++;
    if (sc->retire_by_reacquire) return reacquire_track(sc, index, why, why_size);
    sc->retired++;
    return 0;
}

// 把一张已经上传完成的纹理（SHADER_READ_ONLY_OPTIMAL 布局）缩放 blit 到 swapchain 并 present。
// 源格式/尺寸由调用方给出（texcache 的 image 只带 NVN 格式，VkFormat 由调用方再算一次）。

// 一次性诊断：把上屏源图（真实游戏 RT）整幅读回，落成 raw + 采样日志。
// 只在 debug.s2a.dump_frame=1 时执行一次；不改绘制/呈现语义。
static void dump_present_src(struct nvn_vk_swapchain *sc, VkImage src, uint32_t w, uint32_t h,
                             VkFormat format, const char *dir, int continuous) {
    static int done;
    // 1 = 首次呈现抓一张；2 = 每次呈现都覆盖同一文件（用于和屏幕截图同一时刻对照）。
    if (!dir || !dir[0] || dir[0] != '/') return;
    if (done && !continuous) return;
    done = 1;
    uint32_t bpp = 0;
    switch (format) {
        case VK_FORMAT_R8G8B8A8_UNORM: case VK_FORMAT_R8G8B8A8_SRGB: case VK_FORMAT_R8G8B8A8_SNORM:
        case VK_FORMAT_R8G8B8A8_UINT: case VK_FORMAT_R8G8B8A8_SINT: case VK_FORMAT_B8G8R8A8_UNORM: bpp = 4; break;
        case VK_FORMAT_R5G6B5_UNORM_PACK16: case VK_FORMAT_R5G5B5A1_UNORM_PACK16: case VK_FORMAT_B5G6R5_UNORM_PACK16: bpp = 2; break;
        case VK_FORMAT_R32G32B32A32_SFLOAT: case VK_FORMAT_R32G32B32A32_UINT: case VK_FORMAT_R32G32B32A32_SINT: bpp = 16; break;
        case VK_FORMAT_R16G16B16A16_SFLOAT: case VK_FORMAT_R16G16B16A16_UNORM: bpp = 8; break;
        case VK_FORMAT_R8_UNORM: bpp = 1; break;
        default: break;
    }
    if (!bpp) { __android_log_print(ANDROID_LOG_INFO, "S2Arm", "frame dump: unsupported present format %d", format); return; }
    VkDeviceSize bytes = (VkDeviceSize)w * h * bpp;
    VkBufferCreateInfo bi = { VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO };
    bi.size = bytes; bi.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    VkBuffer buf = VK_NULL_HANDLE; VkDeviceMemory mem = VK_NULL_HANDLE;
    if (vkCreateBuffer(sc->d->device, &bi, NULL, &buf) != VK_SUCCESS) return;
    VkMemoryRequirements mr; vkGetBufferMemoryRequirements(sc->d->device, buf, &mr);
    VkMemoryAllocateInfo mai = { VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO };
    mai.allocationSize = mr.size;
    mai.memoryTypeIndex = s2a_vk_memory_type(sc->d, mr.memoryTypeBits,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, NULL);
    if (vkAllocateMemory(sc->d->device, &mai, NULL, &mem) != VK_SUCCESS) { vkDestroyBuffer(sc->d->device, buf, NULL); return; }
    vkBindBufferMemory(sc->d->device, buf, mem, 0);
    VkCommandBufferAllocateInfo cai = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO };
    cai.commandPool = sc->d->command_pool; cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cai.commandBufferCount = 1;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    if (vkAllocateCommandBuffers(sc->d->device, &cai, &cmd) != VK_SUCCESS) goto cleanup;
    {
        VkCommandBufferBeginInfo cbi = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
        cbi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vkBeginCommandBuffer(cmd, &cbi);
        VkImageMemoryBarrier b = { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER };
        b.oldLayout = VK_IMAGE_LAYOUT_GENERAL; b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
        b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.image = src; b.subresourceRange = (VkImageSubresourceRange){ VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 };
        b.srcAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
        b.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &b);
        VkBufferImageCopy c = {0};
        c.imageSubresource = (VkImageSubresourceLayers){ VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 };
        c.imageExtent = (VkExtent3D){ w, h, 1 };
        vkCmdCopyImageToBuffer(cmd, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, buf, 1, &c);
        b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL; b.newLayout = VK_IMAGE_LAYOUT_GENERAL;
        b.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        b.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0, NULL, 0, NULL, 1, &b);
        vkEndCommandBuffer(cmd);
        VkFenceCreateInfo fci = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
        VkFence fence = VK_NULL_HANDLE;
        vkCreateFence(sc->d->device, &fci, NULL, &fence);
        VkSubmitInfo si = { VK_STRUCTURE_TYPE_SUBMIT_INFO };
        si.commandBufferCount = 1; si.pCommandBuffers = &cmd;
        if (vkQueueSubmit(sc->d->queue, 1, &si, fence) == VK_SUCCESS &&
            vkWaitForFences(sc->d->device, 1, &fence, VK_TRUE, 5000000000ull) == VK_SUCCESS) {
            void *map = NULL;
            if (vkMapMemory(sc->d->device, mem, 0, VK_WHOLE_SIZE, 0, &map) == VK_SUCCESS) {
                char path[1200];
                snprintf(path, sizeof path, "%s/present-src.raw", dir);
                FILE *f = fopen(path, "wb");
                if (f) { size_t wr = fwrite(map, 1, (size_t)bytes, f); fclose(f);
                         __android_log_print(ANDROID_LOG_INFO, "S2Arm",
                             "frame dump: %s %ux%u bpp=%u fmt=%d bytes=%zu written=%zu", path, w, h, bpp, format, (size_t)bytes, wr); }
                const uint8_t *p = (const uint8_t *)map;
                for (uint32_t gy = 0; gy < 4; gy++) for (uint32_t gx = 0; gx < 4; gx++) {
                    size_t off = ((size_t)(gy * (h - 1) / 3) * w + (gx * (w - 1) / 3)) * bpp;
                    uint32_t v0 = 0, v1 = 0;
                    memcpy(&v0, p + off, 4);
                    if (bpp >= 8) memcpy(&v1, p + off + 4, 4);
                    __android_log_print(ANDROID_LOG_INFO, "S2Arm", "frame dump sample (%u,%u)=%08x %08x",
                                        gx * (w - 1) / 3, gy * (h - 1) / 3, v0, v1);
                }
                vkUnmapMemory(sc->d->device, mem);
            }
        }
        if (fence) vkDestroyFence(sc->d->device, fence, NULL);
    }
    if (cmd) vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
cleanup:
    vkDestroyBuffer(sc->d->device, buf, NULL);
    vkFreeMemory(sc->d->device, mem, NULL);
}

// present 内部四段计时：哪一段吃掉这 24ms 必须量出来，不能猜。
// 1=等本帧 GPU 画完(flush fence) 2=acquire 3=录clear+blit并提交 4=queuePresent+fence(含 vsync)
static uint64_t sw_now_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
}
static uint64_t sw_st[4], sw_n;
static void sw_note(int i, uint64_t dt) { if (i >= 0 && i < 4) sw_st[i] += dt; }

// Touch transport is linked by the Android host; older standalone probes may omit it.
extern uint64_t s2a_android_game_touch_epoch(void) __attribute__((weak));
extern void s2a_android_game_touch_viewport_checked(uint64_t, uint64_t, int, int, int, int, int, int) __attribute__((weak));

static void publish_touch(const struct nvn_vk_swapchain *sc, uint64_t epoch,
                          uint32_t crop_w, uint32_t crop_h, int32_t x, int32_t y,
                          uint32_t width, uint32_t height) {
    if (!s2a_android_game_touch_viewport_checked) return;
    // Like the generic compositor, only the standard 16:9 Switch region is known.
    uint64_t a = (uint64_t)crop_w * 9, b = (uint64_t)crop_h * 16;
    uint64_t diff = a > b ? a - b : b - a;
    int standard = diff * 1000 < (uint64_t)crop_h * 9;
    int bw = (int)sc->extent.width, bh = (int)sc->extent.height;
    if (sc->buf_geometry && !sc->geometry_failed && sc->frame_width && sc->frame_height &&
        sc->extent.width && sc->extent.height) {
        // B3：缓冲=渲染目标尺寸，合成器把整幅缓冲放大到 view frame（Java 把 view 布成同比例
        // 居中矩形）。blit dst 矩形在缓冲像素里，按同一比例放大回 view 像素发布——crop
        // 不满缓冲时矩形反映画面在 view 里的真实区域；dst 填满缓冲时退化为整幅 view。
        // 缓冲尺寸没兑现的降级交换链走下面缓冲像素路径，与 blit 同一坐标系。
        x = (int32_t)((int64_t)x * (int32_t)sc->frame_width / (int32_t)sc->extent.width);
        y = (int32_t)((int64_t)y * (int32_t)sc->frame_height / (int32_t)sc->extent.height);
        width = (uint32_t)((int64_t)width * (int32_t)sc->frame_width / (int32_t)sc->extent.width);
        height = (uint32_t)((int64_t)height * (int32_t)sc->frame_height / (int32_t)sc->extent.height);
        bw = (int)sc->frame_width; bh = (int)sc->frame_height;
    }
    s2a_android_game_touch_viewport_checked(epoch, standard ? sc->touch_generation : 0,
        x, y, (int)width, (int)height, bw, bh);
}

// B3：把窗口缓冲改成渲染目标尺寸，SurfaceFlinger/显示硬件把它放大到 view frame。
// 先改缓冲尺寸并核对 getWidth/getHeight 真变了再付销毁+重建的代价：调用失败或返回 0 但
// 缓冲尺寸没动（平台没兑现）时记 geometry_failed 退回普通等比 blit，本帧照常出图，
// 不能整帧失败、也不能每帧销毁重建。
// 旧交换链按正常退休合同销毁（present fence 未证明就失败），新交换链建在改过的缓冲上，
// extent=目标尺寸，present 里的等比 blit 对整幅 crop 退化成 1:1 拷贝。
static int geometry_resize(struct nvn_vk_swapchain *sc, uint32_t w, uint32_t h, char *why, size_t why_size) {
    ANativeWindow *window = sc->window;
    struct s2a_vk_device *d = sc->d;
    uint32_t fw = sc->frame_width, fh = sc->frame_height;
    uint64_t tg = sc->touch_generation;
    // format=0 保留现有像素格式；兑现后 ANativeWindow_getWidth/getHeight 报告的是缓冲尺寸。
    if (ANativeWindow_setBuffersGeometry(window, (int32_t)w, (int32_t)h, 0) != 0) {
        sc->geometry_failed = 1;
        __android_log_print(ANDROID_LOG_WARN, "S2Arm",
            "buffer geometry: setBuffersGeometry(%dx%d) failed — "
            "staying on the aspect-fit blit for this swapchain", (int)w, (int)h);
        return 0;
    }
    if (ANativeWindow_getWidth(window) != (int32_t)w || ANativeWindow_getHeight(window) != (int32_t)h) {
        sc->geometry_failed = 1;
        __android_log_print(ANDROID_LOG_WARN, "S2Arm",
            "buffer geometry: setBuffersGeometry accepted but buffer size is %dx%d, not %ux%u — "
            "platform did not honor it; staying on the aspect-fit blit for this swapchain",
            (int)ANativeWindow_getWidth(window), (int)ANativeWindow_getHeight(window), w, h);
        return 0;
    }
    if (nvn_vk_swapchain_destroy(sc))
        return fail(why, why_size, "buffer geometry resize: previous swapchain has no retirement proof");
    if (nvn_vk_swapchain_create(sc, d, window, why, why_size)) return -1;
    // create 重新 memset：窗口像素与 WSI generation 不属于某一条交换链，交还原值。
    sc->frame_width = fw; sc->frame_height = fh;
    sc->touch_generation = tg;
    if (sc->extent.width != w || sc->extent.height != h) {
        // 驱动按 surface min/max 夹取后 extent 不等于缓冲尺寸：缓冲已改但交换链到不了目标尺寸，
        // 同样降级（继续按 extent 等比 blit，不再重试）。
        sc->geometry_failed = 1;
        __android_log_print(ANDROID_LOG_WARN, "S2Arm",
            "buffer geometry: swapchain extent clamped to %ux%u, not %ux%u — "
            "staying on the aspect-fit blit for this swapchain",
            sc->extent.width, sc->extent.height, w, h);
        return 0;
    }
    __android_log_print(ANDROID_LOG_INFO, "S2Arm",
        "buffer geometry: buffer=%ux%u (render target) frame=%ux%u — compositor/display upscales",
        sc->extent.width, sc->extent.height, fw, fh);
    return 0;
}

int nvn_vk_swapchain_present_image(struct nvn_vk_swapchain *sc, VkImage src, VkFormat src_format,
                                   uint32_t src_w, uint32_t src_h, const int crop[4], uint64_t frame,
                                   char *why, size_t why_size) {
    const uint64_t sw0 = sw_now_ns();
    uint64_t touch_epoch = s2a_android_game_touch_epoch ? s2a_android_game_touch_epoch() : 0;
    // 先完成本帧挂起的 draw（一条 submit + 一次 fence），present 才能读 RT。
    // 失败/超时时命令缓冲与在途资源已保留，这里不能再呈现一张未完成的图。
    if (nvn_vk_first_draw_flush_defer() != 0)
        return fail(why, why_size, "present image: pending draws did not complete (see frame queue log)");
    uint64_t sw1 = sw_now_ns(); sw_note(0, sw1 - sw0);

    if (!sc || !sc->ready || !src) return fail(why, why_size, "present image: not ready");
    if (!src_w || !src_h || src_format == VK_FORMAT_UNDEFINED) return fail(why, why_size, "present image: bad source");
    if (sc->acquire_pending)
        return fail(why, why_size, "present image: previous acquire has no consuming submit; swapchain unusable");
    if (sc->present_failed)
        return fail(why, why_size, "present image: swapchain present path already failed (window retirement required)");
    // 有效裁剪区 = 游戏真实输出区域；crop 不合法时按整幅源图。crop 只决定下面 blit 的
    // 源矩形与等比适配，不再驱动缓冲尺寸（#203）。
    uint32_t crop_x = 0, crop_y = 0, crop_w = src_w, crop_h = src_h;
    if (crop && crop[2] > 0 && crop[3] > 0 && crop[0] >= 0 && crop[1] >= 0 &&
        (uint32_t)crop[0] + (uint32_t)crop[2] <= src_w && (uint32_t)crop[1] + (uint32_t)crop[3] <= src_h) {
        crop_x = (uint32_t)crop[0]; crop_y = (uint32_t)crop[1];
        crop_w = (uint32_t)crop[2]; crop_h = (uint32_t)crop[3];
    }
    // buffer_geometry 按渲染目标尺寸（src）出图：目标尺寸与上一帧相同才付销毁+重建的
    // 代价——连续两帧稳定 = 真换了渲染目标；逐帧换尺寸的动态分辨率永远稳不下来，留在
    // 旧缓冲上等比 blit，crop/目标逐帧变都不会销毁重建交换链。
    int target_stable = sc->geometry_src_w == src_w && sc->geometry_src_h == src_h;
    sc->geometry_src_w = src_w; sc->geometry_src_h = src_h;
    if (sc->buf_geometry && !sc->geometry_failed && sc->window && target_stable &&
        (sc->extent.width != src_w || sc->extent.height != src_h)) {
        int gr = geometry_resize(sc, src_w, src_h, why, why_size);
        if (gr) return gr;
    }
    g_present_sc = sc;
    // B2：每帧机会式排干已到上屏时刻（回报通常滞后 1-2 帧，不阻塞）。
    nvn_vk_dt_poll(sc->d->device, sc->swapchain);
    // 先机会式回收上一帧已 signal 的 present fence；仍在途不是错误（FIFO 可能等下一个 vsync）。
    if (sc->npresent_pending && nvn_vk_swapchain_retire_present(sc, 0) < 0)
        return present_fail_state(sc, why, why_size, "present-retirement fence status is unusable");
    uint32_t index = 0;
    // acquire 有界：窗口被撤销/后台时呈现引擎可能不再归还图像，不能拿 UINT64_MAX 把它拖成
    // “surfaceDestroyed 等满 1 s 后 abort”。超时没有取到 image，本帧绘制已完成，按跳过上屏收尾。
    VkResult ar = acquire_image(sc, &index);
    if (ar == VK_TIMEOUT || ar == VK_NOT_READY) {
        sc->acquire_timeout++;
        if (sc->acquire_timeout % 8 == 1)
            __android_log_print(ANDROID_LOG_INFO, "S2Arm",
                                "present image: acquire timed out (%llu total); frame completed, present skipped",
                                (unsigned long long)sc->acquire_timeout);
        nvn_vk_first_draw_present_done();
        return 1;
    }
    if (ar != VK_SUCCESS && ar != VK_SUBOPTIMAL_KHR)
        return present_fail_state(sc, why, why_size, "vkAcquireNextImageKHR failed");
    if (index >= sc->image_count)
        return present_fail_state(sc, why, why_size, "swapchain image index out of range");
    sc->acquire_pending = 1;   // 提交成功前不能销毁/复用 acquire 信号量
    uint64_t sw2 = sw_now_ns(); sw_note(1, sw2 - sw1);
    VkCommandBufferAllocateInfo cai = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO };
    cai.commandPool = sc->d->command_pool; cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cai.commandBufferCount = 1;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    if (vkAllocateCommandBuffers(sc->d->device, &cai, &cmd) != VK_SUCCESS)
        return present_fail_state(sc, why, why_size, "command buffer allocation failed");
    VkCommandBufferBeginInfo bi = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vkBeginCommandBuffer(cmd, &bi);
    VkImageMemoryBarrier src_barrier = { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER };
    src_barrier.oldLayout = VK_IMAGE_LAYOUT_GENERAL;
    src_barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    src_barrier.srcQueueFamilyIndex = src_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    src_barrier.image = src;
    src_barrier.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    src_barrier.srcAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
    src_barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    // TRANSFER_WRITE 只由 TRANSFER 阶段支持；只写 FRAGMENT_SHADER 会触发
    // VUID-vkCmdPipelineBarrier-pImageMemoryBarriers-02819（8E VVL 实测）。
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 0, NULL, 0, NULL, 1, &src_barrier);
    // blit 完把源图放回 GENERAL，下一次清屏/拷贝/上屏继续在同一个布局上接力。
    VkImageMemoryBarrier src_back = src_barrier;
    src_back.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    src_back.newLayout = VK_IMAGE_LAYOUT_GENERAL;
    src_back.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    src_back.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
    VkImageMemoryBarrier dst_barrier = src_barrier;
    dst_barrier.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    dst_barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    dst_barrier.image = sc->images[index];
    dst_barrier.srcAccessMask = 0;
    dst_barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 0, NULL, 0, NULL, 1, &dst_barrier);
    {
        // 目标先清黑：等比居中后两侧/上下的黑边是明确的，不是上一帧残留。
        VkClearColorValue black = { { 0.0f, 0.0f, 0.0f, 1.0f } };
        VkImageSubresourceRange range = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 };
        vkCmdClearColorImage(cmd, sc->images[index], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &black, 1, &range);
    }
    VkImageBlit blit = {0};
    blit.srcSubresource = (VkImageSubresourceLayers){VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    blit.srcOffsets[0] = (VkOffset3D){(int32_t)crop_x, (int32_t)crop_y, 0};
    blit.srcOffsets[1] = (VkOffset3D){(int32_t)(crop_x + crop_w), (int32_t)(crop_y + crop_h), 1};
    blit.dstSubresource = (VkImageSubresourceLayers){VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    // 等比缩放并居中（同 iOS present_encode / Ryujinx Present 的 crop→fit→center）；目标尺寸是
    // Identity 契约下的发布窗口坐标，不能拿旋转前的宽高算比例。
    uint32_t dst_w = sc->extent.width, dst_h = sc->extent.height;
    uint32_t draw_w, draw_h;   // crop_w/h 已由上面的 src_w/src_h 校验保证非零
    if ((uint64_t)crop_w * dst_h >= (uint64_t)crop_h * dst_w) {
        draw_w = dst_w;
        draw_h = (uint32_t)(((uint64_t)crop_h * dst_w + crop_w / 2) / crop_w);
    } else {
        draw_h = dst_h;
        draw_w = (uint32_t)(((uint64_t)crop_w * dst_h + crop_h / 2) / crop_h);
    }
    if (draw_w > dst_w) draw_w = dst_w;
    if (draw_h > dst_h) draw_h = dst_h;
    int32_t dst_x = (int32_t)((dst_w - draw_w) / 2), dst_y = (int32_t)((dst_h - draw_h) / 2);
    blit.dstOffsets[0] = (VkOffset3D){ dst_x, dst_y, 0 };
    blit.dstOffsets[1] = (VkOffset3D){ dst_x + (int32_t)draw_w, dst_y + (int32_t)draw_h, 1 };
    {
        static int logged;
        if (!logged) {
            logged = 1;
            __android_log_print(ANDROID_LOG_INFO, "S2Arm",
                "present image: src=%ux%u crop=(%d,%d,%d,%d)->(%u,%u,%u,%u) dst=%ux%u fit=%ux%u at=(%d,%d) preTransform=identity src_fmt=%d",
                src_w, src_h, crop ? crop[0] : 0, crop ? crop[1] : 0, crop ? crop[2] : 0, crop ? crop[3] : 0,
                crop_x, crop_y, crop_w, crop_h, dst_w, dst_h, draw_w, draw_h, dst_x, dst_y, (int)src_format);
        }
    }
    nvn_vk_callring_add("blit dst_index=%u dst=%p %ux%u", (unsigned)index, (void *)sc->images[index], (unsigned)sc->extent.width, (unsigned)sc->extent.height);
    vkCmdBlitImage(cmd, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, sc->images[index],
                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &blit, VK_FILTER_LINEAR);
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                         0, 0, NULL, 0, NULL, 1, &src_back);
    VkImageMemoryBarrier to_present = dst_barrier;
    to_present.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    to_present.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    to_present.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    to_present.dstAccessMask = 0;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                         0, 0, NULL, 0, NULL, 1, &to_present);
    nvn_vk_first_draw_gpu_end(cmd);   // 诊断：GPU 帧结束时间戳（present blit 末尾）
    vkEndCommandBuffer(cmd);
    VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_TRANSFER_BIT;
    VkSubmitInfo submit = { VK_STRUCTURE_TYPE_SUBMIT_INFO };
    submit.waitSemaphoreCount = 1; submit.pWaitSemaphores = &sc->acquire; submit.pWaitDstStageMask = &wait_stage;
    submit.commandBufferCount = 1; submit.pCommandBuffers = &cmd;
    submit.signalSemaphoreCount = 1; submit.pSignalSemaphores = &sc->release[index];
    if (sc->fence) {
        vkResetFences(sc->d->device, 1, &sc->fence);
    } else {
        VkFenceCreateInfo fci = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
        if (vkCreateFence(sc->d->device, &fci, NULL, &sc->fence) != VK_SUCCESS)
            return present_fail_state(sc, why, why_size, "draw-completion fence creation failed");
    }
    const int fault = present_fault_mode();
    // 呈现退休 fence 必须与渲染 fence 是两个对象：Vulkan 要求 present fence 传入时未 signal、
    // 且没有被其它队列操作使用。用渲染 fence 冒充 present fence 会在提交完成时就 signal，
    // 既不能证明呈现退休，也违反 present fence 的有效性要求。
    VkFence present_fence = VK_NULL_HANDLE;
    if (fault != 2 && !sc->retire_by_reacquire) {   // 再 acquire 模式没有 present fence 可带
        VkFenceCreateInfo pfci = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
        if (vkCreateFence(sc->d->device, &pfci, NULL, &present_fence) != VK_SUCCESS) {
            // 还没提交：命令缓冲可回收；acquire 已被消费，呈现路径仍按失败终态处理。
            vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
            sc->submit_failed++;
            return present_fail_state(sc, why, why_size,
                                      "present-retirement fence creation failed; command buffer reclaimed");
        }
    }
    if (vkQueueSubmit(sc->d->queue, 1, &submit, sc->fence) != VK_SUCCESS) {
        // 没有提交成功 = 命令从未执行：命令缓冲与 fence 可以回收。但 acquire 已被消费，
        // 这份 swapchain 的 acquire/release 信号量状态不再能保证，呈现路径按失败终态处理。
        if (present_fence) vkDestroyFence(sc->d->device, present_fence, NULL);
        vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
        sc->submit_failed++;
        return present_fail_state(sc, why, why_size,
                                  "vkQueueSubmit failed; command buffer/fences reclaimed, acquire state kept");
    }
    sc->acquire_pending = 0;   // 提交已 wait acquire 信号量
    { uint64_t sw3 = sw_now_ns(); sw_note(2, sw3 - sw2); sw2 = sw3; }
    if (fault == 1) {
        // 负例：真实提交成功，但 present 报错。命令与 fence 仍在队列上，保留且不调用 present_done。
        present_retain(sc, sc->fence, present_fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        sc->present_failed_count++;
        return present_fail_state(sc, why, why_size,
                                  "vkQueuePresentKHR failed after submit; command buffer/fences retained, "
                                  "present_done not called");
    }
    VkSwapchainPresentFenceInfoEXT fence_info = { VK_STRUCTURE_TYPE_SWAPCHAIN_PRESENT_FENCE_INFO_EXT };
    fence_info.swapchainCount = 1;
    fence_info.pFences = &present_fence;
    VkPresentInfoKHR pi = { VK_STRUCTURE_TYPE_PRESENT_INFO_KHR };
    if (present_fence) pi.pNext = &fence_info;   // fault=2：故意不带 present fence，验证“退休未证明”保留
    pi.waitSemaphoreCount = 1; pi.pWaitSemaphores = &sc->release[index];
    pi.swapchainCount = 1; pi.pSwapchains = &sc->swapchain; pi.pImageIndices = &index;
    // B2：display_timing 计量时给 present 挂 presentID（frame 由调用方给，0 = 不挂）。
    VkPresentTimesInfoGOOGLE dt_info;
    VkPresentTimeGOOGLE dt_time;
    nvn_vk_dt_chain(&pi, &dt_info, &dt_time, frame);
    VkResult pr = pPresent(sc->d->queue, &pi);
    if (pr != VK_SUCCESS && pr != VK_SUBOPTIMAL_KHR) {
        // 提交成功后 present 报错：渲染命令可能仍在执行，命令缓冲/fence 都不能释放/复用。
        present_retain(sc, sc->fence, present_fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        sc->present_failed_count++;
        return present_fail_state(sc, why, why_size,
                                  "vkQueuePresentKHR failed after submit; command buffer/fences retained, "
                                  "present_done not called");
    }
    sc->submitted++;
    {
        // 整幅源图读回放在两条路径汇合处：延迟 present 在下面提前 return，
        // 留在末尾会让 dump_frame 在默认路径下永远够不着（cure3 实测零输出）。
        char frame_dump[PROP_VALUE_MAX] = {0};
        __system_property_get("debug.s2a.dump_frame", frame_dump);
        char dir[PROP_VALUE_MAX] = {0};
        __system_property_get("debug.s2a.dump_fail", dir);
        if ((frame_dump[0] == '1' || frame_dump[0] == '2') && dir[0] == '/')
            dump_present_src(sc, src, src_w, src_h, src_format, dir, frame_dump[0] == '2');
    }
    if (nvn_vk_first_draw_present_deferred()) {
        // 延迟 present：不等渲染 fence。fence 与命令缓冲交给 first_draw 的延迟槽，
        // 下一帧 present 前统一等/释放；本帧的 pin 也留到那时再解。
        nvn_vk_first_draw_slot_blit(sc->fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING) (void)nvn_vk_swapchain_retire_present(sc, 0);
        if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING)
            (void)nvn_vk_swapchain_retire_present(sc, NVK_PRESENT_RETIRE_BUDGET_NS);
        if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING) {
            present_retain(sc, VK_NULL_HANDLE, present_fence, VK_NULL_HANDLE);
            sc->retire_timeout++;
            return present_fail_state(sc, why, why_size,
                                      "present-retirement tracking table is full (deferred present); "
                                      "present fence retained, presentation retirement not claimed");
        }
        pending_push(sc, present_fence, sc->retire_by_reacquire ? (int)index : -1);
        (void)nvn_vk_swapchain_retire_present(sc, 0);
        present_account_tick(sc);
        publish_touch(sc, touch_epoch, crop_w, crop_h, dst_x, dst_y, draw_w, draw_h);
        { uint64_t sw3 = sw_now_ns(); sw_note(3, sw3 - sw2);
          if (++sw_n % 32 == 0)
              __android_log_print(ANDROID_LOG_INFO, "S2Arm",
                  "present 拆分(延迟)：等GPU画完=0 us | acquire=%llu us | 录blit+提交=%llu us | present+vsync=%llu us",
                  (unsigned long long)(sw_st[1] / sw_n / 1000), (unsigned long long)(sw_st[2] / sw_n / 1000),
                  (unsigned long long)(sw_st[3] / sw_n / 1000));
          if (sw_n % 32 == 0) { memset(sw_st, 0, sizeof sw_st); sw_n = 0; } }
        return 0;
    }
    // 1) 绘制完成：渲染 fence signal 只证明这批命令执行完。同步调度下等到它才解锁本帧纹理 pin
    //    并回收命令缓冲；这与“呈现退休”是两件事。
    int drew = vkWaitForFences(sc->d->device, 1, &sc->fence, VK_TRUE, 2000000000ull) == VK_SUCCESS;
    if (!drew) {
        present_retain(sc, sc->fence, present_fence, cmd);
        sc->fence = VK_NULL_HANDLE;
        sc->fence_timeout++;
        return present_fail_state(sc, why, why_size,
                                  "draw-completion fence timeout; command buffer/fences retained, "
                                  "presentation retirement not claimed");
    }
    sc->completed++;
    nvn_vk_first_draw_gpu_read(sc->d, "present");
    nvn_vk_first_draw_present_done();
    vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &cmd);
    { uint64_t sw3 = sw_now_ns(); sw_note(3, sw3 - sw2);
      if (++sw_n % 32 == 0)
          __android_log_print(ANDROID_LOG_INFO, "S2Arm",
              "present 拆分：等GPU画完=%llu us | acquire=%llu us | 录blit+提交=%llu us | present+vsync=%llu us",
              (unsigned long long)(sw_st[0] / sw_n / 1000), (unsigned long long)(sw_st[1] / sw_n / 1000),
              (unsigned long long)(sw_st[2] / sw_n / 1000), (unsigned long long)(sw_st[3] / sw_n / 1000));
      if (sw_n % 32 == 0) { memset(sw_st, 0, sizeof sw_st); sw_n = 0; } }
    // 2) 呈现退休：present fence 进入 pending 队列，由下一帧/撤销/Stop 观察。这里不能同步等：
    //    FIFO 的 present fence 可能在下一个 vsync 才 signal，逐帧同步等会把渲染节拍绑在呈现引擎上。
    if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING) {
        (void)nvn_vk_swapchain_retire_present(sc, 0);   // 先摘掉已 signal 的
    }
    if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING) {
        // 表满：先给最老的 present 一个有界机会退休（合成器/后台切换可能短暂压住）；
        // 仍腾不出位置就不能丢 present fence，进入失败终态并把它放进保留账本。
        (void)nvn_vk_swapchain_retire_present(sc, NVK_PRESENT_RETIRE_BUDGET_NS);
    }
    if (sc->npresent_pending >= NVK_SC_PRESENT_PENDING) {
        present_retain(sc, VK_NULL_HANDLE, present_fence, VK_NULL_HANDLE);
        sc->retire_timeout++;
        return present_fail_state(sc, why, why_size,
                                  "present-retirement tracking table is full and no bounded wait retired a frame; "
                                  "present fence retained, presentation retirement not claimed");
    }
    pending_push(sc, present_fence, sc->retire_by_reacquire ? (int)index : -1);
    (void)nvn_vk_swapchain_retire_present(sc, 0);   // 机会式回收已 signal 的旧 present fence
    present_account_tick(sc);
    publish_touch(sc, touch_epoch, crop_w, crop_h, dst_x, dst_y, draw_w, draw_h);
    return 0;
}

int nvn_vk_swapchain_destroy(struct nvn_vk_swapchain *sc) {
    if (!sc || !sc->d) return 0;
    if (!sc->swapchain && !sc->surface && !sc->fence && !sc->acquire_fence && !sc->npresent_pending &&
        !sc->nretained && !sc->acquire_pending) return 0;
    if (sc->acquire_pending) {
        // acquire 已经成功、但没有任何提交去 wait 那把信号量：销毁信号量/交换链没有完成证明。
        // 保留全部对象与 WSI 租约，按失败终态结束；不伪造 acquire 已完成。
        sc->present_failed = 1;
        snprintf(sc->present_fail, sizeof sc->present_fail,
                 "acquired swapchain image has no submit consuming its acquire semaphore");
        __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                            "swapchain destroy: acquired image has no acquire-completion proof; "
                            "swapchain/surface/semaphores kept");
        sc->ready = 0;
        return -1;
    }
    __android_log_print(ANDROID_LOG_INFO, "S2Arm",
                        "present lifecycle: submitted=%llu draw_completed=%llu presentation_retired=%llu "
                        "pending=%d retained=%d acquire_timeout=%llu (%s proves retirement, not scanout)",
                        (unsigned long long)sc->submitted, (unsigned long long)sc->completed,
                        (unsigned long long)sc->retired, sc->npresent_pending, sc->nretained,
                        (unsigned long long)sc->acquire_timeout,
                        sc->retire_by_reacquire ? "same-image reacquire" : "present fence");
    // 最后一次观察呈现退休：没有后续帧的 Stop/窗口撤销/旋转都从这里收口。预算有界；
    // 拿不到证明就保留 swapchain/surface/present fence，不 ACK 窗口、不伪造“已退休”。
    int64_t retire_deadline = fd_now_ns_sc() + NVK_PRESENT_RETIRE_BUDGET_NS;
    if (sc->retire_by_reacquire && sc->acquire_fence_armed) {
        // 已交给 acquire 的 fence 在触发前不能销毁：先有界收一次账（顺带退休被它交回的那张图）。
        int64_t left = retire_deadline - fd_now_ns_sc();
        int orr = reacquire_observe(sc, left > 0 ? left : 0);
        if (orr != 0) {
            sc->retire_timeout++;
            sc->present_failed = 1;
            snprintf(sc->present_fail, sizeof sc->present_fail, "acquire fence not observed at window retirement");
            __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                                "swapchain destroy: acquire fence %s within %lld ms; swapchain/surface kept",
                                orr < 0 ? "status unusable" : "still unsignaled",
                                (long long)(NVK_PRESENT_RETIRE_BUDGET_NS / 1000000));
            sc->ready = 0;
            return -1;
        }
    }
    // 再 acquire 模式下仍在 pending 的呈现（至少屏上那张，它不会再被交回）没有 acquire 证明；
    // 先等在途命令完成，再按队列空闲假设收口（见下）。present fence 模式照旧在这里要证明。
    if (sc->npresent_pending && !sc->retire_by_reacquire) {
        int64_t left = retire_deadline - fd_now_ns_sc();
        int rr = nvn_vk_swapchain_retire_present(sc, left > 0 ? left : 0);
        if (rr != 0) {
            sc->retire_timeout++;
            sc->present_failed = 1;
            snprintf(sc->present_fail, sizeof sc->present_fail,
                     "presentation retirement not proven at window retirement");
            __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                                "swapchain destroy: %d presentation(s) have no retirement proof within %lld ms "
                                "(%s); swapchain/surface kept",
                                sc->npresent_pending, (long long)(NVK_PRESENT_RETIRE_BUDGET_NS / 1000000),
                                rr < 0 ? "fence status unusable" : "present fence still unsignaled");
            sc->ready = 0;
            return -1;
        }
    }
    const int had_retained = sc->retire_by_reacquire ? sc->nretained : 0;
    if (sc->nretained) {
        // 窗口退休前再给一次有界完成检查：拿到完成就显式回收命令缓冲/fence，再正常销毁 swapchain。
        int64_t deadline = retire_deadline;   // B7：与 pending 退休共用一个总预算，不各给 500 ms
        int settled = 1;
        for (int i = 0; i < sc->nretained && settled; i++) {
            VkFence fences[2] = { sc->retained[i].fence, sc->retained[i].present_fence };
            for (int k = 0; k < 2; k++) {
                if (!fences[k]) continue;   // 没有该类证明：不能当完成，也不能等一个空 fence
                int64_t left = deadline - fd_now_ns_sc();
                if (left <= 0) { settled = 0; break; }
                if (vkWaitForFences(sc->d->device, 1, &fences[k], VK_TRUE, (uint64_t)left) != VK_SUCCESS) {
                    settled = 0;
                    break;
                }
            }
        }
        if (settled) {
            for (int i = 0; i < sc->nretained; i++) {
                if (sc->retained[i].fence) vkDestroyFence(sc->d->device, sc->retained[i].fence, NULL);
                if (sc->retained[i].present_fence) vkDestroyFence(sc->d->device, sc->retained[i].present_fence, NULL);
                if (sc->retained[i].cmd) vkFreeCommandBuffers(sc->d->device, sc->d->command_pool, 1, &sc->retained[i].cmd);
            }
            __android_log_print(ANDROID_LOG_INFO, "S2Arm",
                                "swapchain destroy: %d retained present command set(s) completed; reclaimed explicitly",
                                sc->nretained);
            sc->nretained = 0;
        } else {
            // 已提交但没有完成/呈现证明：命令缓冲/fence（以及它们引用的 swapchain 图像）不能销毁。
            // 按窗口退休合同保留，随设备/实例销毁；不伪造 presentation 已退休。
            sc->present_failed = 1;
            snprintf(sc->present_fail, sizeof sc->present_fail,
                     "in-flight present command set has no completion proof at window retirement");
            __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                                "swapchain destroy: %d in-flight present command set(s) retained "
                                "(no completion/presentation proof); swapchain/surface kept",
                                sc->nretained);
            sc->ready = 0;
            return -1;
        }
    }
    if (sc->retire_by_reacquire && (sc->npresent_pending || had_retained)) {
        // 没有 maintenance1 时规范给不出最后几次呈现的完成证明（屏上那张图不会再被 acquire 交回）。
        // 与通用路径 RetirementLedger 的 CanAssumeQueueIdle 同一条兼容假设：所有提交都已完成之后，
        // 呈现队列 vkQueueWaitIdle 返回成功，才视作呈现引擎已不再等待这些 release 信号量。
        // 这是有名字的假设，不是呈现完成证明：单独计 idle_assumed，不算进 retired；失败就保留对象。
        int n = sc->npresent_pending + had_retained;
        VkResult idle = vkQueueWaitIdle(sc->d->queue);
        if (idle != VK_SUCCESS) {
            sc->present_failed = 1;
            snprintf(sc->present_fail, sizeof sc->present_fail,
                     "queue idle failed at window retirement without presentation proof");
            __android_log_print(ANDROID_LOG_ERROR, "S2Arm",
                                "swapchain destroy: vkQueueWaitIdle=%d; %d presentation(s) without reacquire proof, "
                                "swapchain/surface kept", (int)idle, n);
            sc->ready = 0;
            return -1;
        }
        sc->idle_assumed += (uint64_t)n;
        sc->npresent_pending = 0;
        __android_log_print(ANDROID_LOG_WARN, "S2Arm",
                            "swapchain destroy: retirement evidence=queue-idle-assumed for %d presentation(s) "
                            "(no VK_EXT_swapchain_maintenance1; last image never re-acquired); present completion not proven",
                            n);
    }
    // 初始 summary 在等待前打印；成功收口后再打一行终态，证明“无后续帧”也把 pending
    // 等到/退休到 0，而不是只凭初始 pending=1 猜结果。
    __android_log_print(ANDROID_LOG_INFO, "S2Arm",
                        "present lifecycle settled: submitted=%llu presentation_retired=%llu idle_assumed=%llu "
                        "pending=%d retained=%d (no-next-frame retirement)",
                        (unsigned long long)sc->submitted, (unsigned long long)sc->retired,
                        (unsigned long long)sc->idle_assumed, sc->npresent_pending, sc->nretained);
    if (sc->acquire) vkDestroySemaphore(sc->d->device, sc->acquire, NULL);
    for (uint32_t i = 0; i < sc->image_count && i < 8; i++)
        if (sc->release[i]) vkDestroySemaphore(sc->d->device, sc->release[i], NULL);
    if (sc->fence) vkDestroyFence(sc->d->device, sc->fence, NULL);
    if (sc->acquire_fence) vkDestroyFence(sc->d->device, sc->acquire_fence, NULL);
    nvn_vk_dt_detach();   // B2：presentID/时刻账随本 swapchain 一并作古
    if (sc->swapchain && pDestroySwapchain) pDestroySwapchain(sc->d->device, sc->swapchain, NULL);
    if (sc->surface) vkDestroySurfaceKHR(sc->d->instance, sc->surface, NULL);
    if (g_present_sc == sc) g_present_sc = NULL;
    sc->swapchain = VK_NULL_HANDLE;
    sc->surface = VK_NULL_HANDLE;
    sc->fence = VK_NULL_HANDLE;
    sc->acquire_fence = VK_NULL_HANDLE;
    sc->acquire_fence_armed = 0;
    sc->npresent_pending = 0;
    sc->ready = 0;
    return 0;
}
