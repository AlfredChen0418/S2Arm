// 开发浮层（runtime/src/hud.c，S2A_HUD=1）：界面每 0.5 秒取一次诊断文字。宿主侧拿不到的项由界面传入：
//   发热档、电量（0–1，取不到给 -1）与变化速率（%/分，取不到给 NAN）、屏幕像素宽高、包号
#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
// metal_hud_requested 表示启动配置请求官方 HUD，不代表已观察到它显示；S2Arm 仍显示自采 FPS 与内存。
typedef struct { const char *thermal, *build_id; double battery_rate_pm, screen_w, screen_h, battery_level; int metal_hud_requested; } s2a_hud_host;
int s2a_hud_on(void);
void s2a_hud_frame(void);                                              // 每上屏一帧（ap_host.c s2a_frame_presented）
void s2a_hud_audio(int session, int64_t gap_ms_total, int64_t skipped);   // 声音诊断 10 秒汇报（S2AOutputDiag.cs）
int s2a_hud_text(char *out, size_t n, const s2a_hud_host *h);
#ifdef __cplusplus
}
#endif
