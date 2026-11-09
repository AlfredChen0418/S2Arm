// S2Arm 内部分辨率（S2A_RES_SCALE：iOS < 1，Mac 0.25–2，10-30 帧率能耗线；语义照 Ryujinx 分辨率缩放，MIT）：可缩的渲染目标的 Metal 存储按比例 s 缩小，游戏看不见（贴图查询、
//   游戏常量、游戏坐标都是原尺寸），换算只在回放层碰 Metal 的地方做：视口 / 裁剪 / 清除区域、片元坐标与取图倍率（支持缓冲 render_scale / render_scale_y）、
//   拷贝区域、CPU 读写、上屏裁剪。比例为 1（缺省）时这里每个入口第一句就返回，行为与没有本文件时逐字节相同。
// 单位：游戏坐标 →（宽屏层换算）→ 画布坐标 →（× s）→ 存储坐标。画布 = 不缩时这张存储该有的尺寸（宽屏放宽过的就是放宽尺寸），存储 = ⌈画布 × s⌉（Ryujinx GetCreateInfo 取整）。
// 哪些存储缩（rs_eligible，同 Ryujinx IsUpscaleCompatible + UpscaleSafeMode，再加直连自己的条件）：
//   - 游戏标了可压缩（NVN 给渲染目标的标志）、没标上屏 / 视频解码 / 存储图像 / 线性 / 稀疏；二维、二维数组、矩形（不缩多重采样：缩放拷贝画不了它）；
//   - 缩放拷贝两个方向都画得了的格式（copy_t2t_draw：浮点 / 归一化颜色、D32F、D32F+S8；不缩整数格式和 D16）；不是随包 / 缓冲 / 视图 / 压缩 / 3D（tex_widen_why）；
//   - Ryujinx 安全尺寸：级数 ≤ 3、宽高 ≥ 8、非深度且多通道的不缩方形、高 < 360 的 16:9 不缩（模糊 / 泛光）、宽 = 高² 的不缩（二维当三维的查色表）。
// 什么时候缩：第一次当渲染目标时（rs_rts，开遍前；同 Ryujinx「可缩 → 当目标时缩」），同一遍的附件比例一致：有一个不可缩 / 被拉黑，全遍退回 1。
// 退回原尺寸（拉黑，Ryujinx BlacklistScale）：缩放图被顶点 / 计算阶段读、被存储图像槽绑、被句柄数组读、被不精确的片元着色器（scale_exact ≠ 1）读、
//   被缓冲 → 贴图拷贝写（CPU 写入：Ryujinx 部分写入即拉黑）、当多重采样解析的目标。前三类是绑定时发现的，记进待办，下一个安全点（开遍前 / 派发前）就换回原尺寸存储。
#import <objc/runtime.h>

static double g_rs = -1;   // 设定比例（S2A_RES_SCALE；1 = 关）
static double rs_setting(void) {
    if (g_rs < 0) {
        const char *e = getenv("S2A_RES_SCALE"); double v = e && *e ? atof(e) : 1.0;
        if (!(v > 0)) v = 1.0;
#if TARGET_OS_IPHONE
        if (v > 1.0) { MLOG("内部分辨率：S2A_RES_SCALE=%s 大于 1，iOS 只缩小不放大，按 1", e); v = 1.0; }
#else
        if (v > 2.0) { MLOG("内部分辨率：S2A_RES_SCALE=%s 大于 2，按 2", e); v = 2.0; }
#endif
        if (v < 0.25) { MLOG("内部分辨率：S2A_RES_SCALE=%s 小于 0.25，按 0.25", e); v = 0.25; }
        g_rs = v;
        if (v != 1.0) MLOG("内部分辨率：可缩的渲染目标按 %.3f 画（S2A_RES_SCALE；游戏看不见，上屏缩放回窗口）", v);
    }
    return g_rs;
}
static inline int rs_on(void) { return (g_rs < 0 ? rs_setting() : g_rs) != 1.0; }

// 每份存储的缩放记录挂在 Metal 根贴图上（关联对象：存储释放时一起走，地址复用不会认错）
enum { RS_NONE = 0, RS_SCALED = 1, RS_BLACK = 2 };
@interface NvRsInfo : NSObject { @public int state; NSUInteger cw, ch; } @end
@implementation NvRsInfo @end
static char k_rs_info;
static id<MTLTexture> rs_root(id<MTLTexture> t0) { id<MTLTexture> t = t0; while (t.parentTexture) t = t.parentTexture; return t; }
static NvRsInfo *rs_info(id<MTLTexture> t) { return t ? objc_getAssociatedObject(rs_root(t), &k_rs_info) : nil; }
static int rs_state(id<MTLTexture> t) { if (!t || !rs_on()) return RS_NONE; NvRsInfo *i = rs_info(t); return i ? i->state : RS_NONE; }
static void rs_set_info(id<MTLTexture> t, int state, NSUInteger cw, NSUInteger ch) {
    NvRsInfo *i = [NvRsInfo new]; i->state = state; i->cw = cw; i->ch = ch;
    objc_setAssociatedObject(rs_root(t), &k_rs_info, i, OBJC_ASSOCIATION_RETAIN);
}
// 这份存储相对画布的比例（没缩 = 1）
static double rs_of(id<MTLTexture> t) { return rs_state(t) == RS_SCALED ? g_rs : 1.0; }
// 画布尺寸（第 level 级，相对 t 自己的级；没缩 = 存储尺寸）
static NSUInteger rs_mip(NSUInteger v, NSUInteger level) { return level < sizeof(v) * 8 ? MAX(v >> level, 1u) : 1; }
static void rs_canvas(id<MTLTexture> t, NSUInteger level, NSUInteger *w, NSUInteger *h) {
    NvRsInfo *i = rs_state(t) == RS_SCALED ? rs_info(t) : nil;
    if (!i) { *w = rs_mip(t.width, level); *h = rs_mip(t.height, level); return; }
    NSUInteger rl = level; for (id<MTLTexture> p = t; p.parentTexture; p = p.parentTexture) rl += p.parentRelativeLevel;
    *w = rs_mip(i->cw, rl); *h = rs_mip(i->ch, rl);
}
// 这个对象（贴图或视图）自己第 0 级的画布宽 / 高（宽屏胶水比较「放没放宽」用画布，不用存储）
static NSUInteger rs_cw(id<MTLTexture> t) { NSUInteger w, h; if (!t) return 0; rs_canvas(t, 0, &w, &h); return w; }
static NSUInteger rs_ch(id<MTLTexture> t) { NSUInteger w, h; if (!t) return 0; rs_canvas(t, 0, &w, &h); return h; }
static NSUInteger rs_up(NSUInteger v, double s) { NSUInteger r = (NSUInteger)ceil((double)v * s - 1e-9); return r ? r : 1; }   // Ryujinx：⌈v·s⌉
// 画布矩形 → 存储矩形（起点向下取整、终点向上取整，盖住画布矩形碰到的每个存储像素；同 Ryujinx 裁剪缩放）
static void rs_rect(double s, long *x, long *y, long *w, long *h) {
    if (s == 1.0) return;
    long x0 = (long)floor(*x * s), y0 = (long)floor(*y * s), x1 = (long)ceil((*x + *w) * s - 1e-9), y1 = (long)ceil((*y + *h) * s - 1e-9);
    *x = x0; *y = y0; *w = x1 - x0; *h = y1 - y0;
}

static const char *tex_widen_why(const tex_t *t);
static int fmt_int_or_ds(MTLPixelFormat f);
// 单通道颜色格式（Ryujinx 不缩方形的规则对它放行，同深度）
static int rs_one_comp(MTLPixelFormat f) {
    switch (f) { case MTLPixelFormatR8Unorm: case MTLPixelFormatR8Snorm: case MTLPixelFormatR16Unorm: case MTLPixelFormatR16Snorm: case MTLPixelFormatR16Float: case MTLPixelFormatR32Float: case MTLPixelFormatA8Unorm: return 1; default: return 0; }
}
// 这张贴图的存储能不能缩（见文件头）；why 给不能缩的原因（日志用）
static int rs_eligible(const tex_t *t, id<MTLTexture> base, const char **why) {
    const char *w = NULL;
    uint32_t bad = NVN_TEXTURE_FLAGS_DISPLAY | NVN_TEXTURE_FLAGS_VIDEO_DECODE | NVN_TEXTURE_FLAGS_IMAGE | NVN_TEXTURE_FLAGS_LINEAR | NVN_TEXTURE_FLAGS_SPARSE | NVN_TEXTURE_FLAGS_LINEAR_RENDER_TARGET;
    if (!t || !base) { if (why) *why = "没有存储"; return 0; }
    MTLPixelFormat pf = base.pixelFormat; int depth = pf == MTLPixelFormatDepth32Float || pf == MTLPixelFormatDepth32Float_Stencil8;
    NSUInteger gw = t->d.width, gh = t->d.height;
    if (!(t->d.flags & NVN_TEXTURE_FLAGS_COMPRESSIBLE) || (t->d.flags & bad)) w = "标志（不可压缩 / 上屏 / 视频 / 存储图像 / 线性 / 稀疏）";
    else if (t->d.target != NVN_TEXTURE_TARGET_2D && t->d.target != NVN_TEXTURE_TARGET_2D_ARRAY && t->d.target != NVN_TEXTURE_TARGET_RECTANGLE) w = "类型（只缩二维 / 二维数组 / 矩形）";
    else if (base.sampleCount > 1 || base.parentTexture || base.buffer) w = "多重采样 / 视图 / 缓冲";
    else if (tex_widen_why(t)) w = tex_widen_why(t);
    else if (!depth && fmt_int_or_ds(pf)) w = "格式（整数 / D16 / 只有模板：缩放拷贝画不了）";
    else if ((t->d.levels ? t->d.levels : 1) > 3) w = "级数 > 3（多半是游戏贴图）";
    else if (gw < 8 || gh < 8) w = "太小";
    else if (!depth && !rs_one_comp(pf) && gw == gh) w = "方形";
    else if (gh < 360 && fabs((double)gw - gh * 16.0 / 9.0) <= 16) w = "高 < 360 的 16:9（模糊 / 泛光）";
    else if (gw == gh * gh) w = "宽 = 高²（查色表）";
    if (why) *why = w;
    return !w;
}
