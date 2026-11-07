// 手机机身传感器 → NS2 原始坐标（runtime/src/phone_axes.c）：期望按手机姿态手算，不复述实现。
// NS2 原始坐标以手柄正面为基：x 右、y 上沿、z 正面法线，加速度报支撑反力（真报文 report05 静止放平 = (−0.02, 0.14, 1.01) g）；掌机正面即屏幕。
#include "phone_axes.h"
#include <math.h>
#include <stdio.h>
static int bad;
static void near3(const char *what, const float got[3], double x, double y, double z) {
    if (fabs(got[0] - x) > 1e-4 || fabs(got[1] - y) > 1e-4 || fabs(got[2] - z) > 1e-4) {
        printf("✗ %s：期望 (%.4f, %.4f, %.4f) → (%.4f, %.4f, %.4f)\n", what, x, y, z, got[0], got[1], got[2]); bad++; }
}
int main(void) {
    float a[3], g[3]; const double zero[3] = {0, 0, 0}, deg = 57.29577951308232;
    // ① 横屏右竖持（顶端朝左、屏幕朝玩家）：机身 x 朝上，重力沿 −x → 支撑反力沿屏幕上 → raw (0, +1, 0)
    { double acc[3] = {-1, 0, 0}; s2a_phone_axes(1, acc, zero, a, g); near3("① 横屏右竖持 加速度", a, 0, 1, 0); }
    // ② 横屏左竖持（顶端朝右）：机身 x 朝下，重力沿 +x → 同样 raw (0, +1, 0)
    { double acc[3] = {1, 0, 0}; s2a_phone_axes(0, acc, zero, a, g); near3("② 横屏左竖持 加速度", a, 0, 1, 0); }
    // ③ 平放屏幕朝上：重力 (0, 0, −1) → 反力沿出屏 → raw (0, 0, +1)，与 NS2 手柄放平真报文 z ≈ +1.01 g 同号同轴
    { double acc[3] = {0, 0, -1}; s2a_phone_axes(1, acc, zero, a, g); near3("③ 平放朝上 加速度", a, 0, 0, 1); }
    // ④ 横屏右，像方向盘一样往右转（玩家看是顺时针 = 绕出屏轴负向）：机身绕 z 以 −1 rad/s → raw z = −57.3
    { double gyr[3] = {0, 0, -1}; s2a_phone_axes(1, zero, gyr, a, g); near3("④ 横屏右 方向盘右转 角速度", g, 0, 0, -deg); }
    // ⑤ 横屏右，绕屏幕右轴正转 1 rad/s（上沿转向玩家）：屏幕右 = 机身 −y → 机身 y 角速度 −1 → raw x = +57.3
    { double gyr[3] = {0, -1, 0}; s2a_phone_axes(1, zero, gyr, a, g); near3("⑤ 横屏右 绕右轴 角速度", g, deg, 0, 0); }
    // ⑥ 反例：同一组机身读数，横屏左与横屏右的「右」「上」两个分量反号（出屏分量不变）
    { double acc[3] = {0.3, -0.5, -0.8}; float r[3], l[3], gr[3], gl[3];
      s2a_phone_axes(1, acc, acc, r, gr); s2a_phone_axes(0, acc, acc, l, gl);
      float x[3] = {r[0] + l[0], r[1] + l[1], r[2] - l[2]}; near3("⑥ 横屏左右 右 / 上反号、出屏不变", x, 0, 0, 0); }
    // ⑦ 手柄 GCMotion 平放：重力 (0, 0, −1)、没有用户加速度 → 反力沿正面法线 → raw (0, 0, +1)，与 NS2 手柄放平真报文同号
    { double gravity[3] = {0, 0, -1}; s2a_gc_motion_axes(gravity, zero, zero, a, g); near3("⑦ 手柄平放 加速度", a, 0, 0, 1); near3("⑦ 手柄平放 角速度", g, 0, 0, 0); }
    // ⑧ 重力和用户加速度相加：重力 (0, −1, 0) 加用户 (0.25, 0, 0.5) → 取反 (−0.25, +1, −0.5)；角速度 (1, −2, 0.5) rad/s 换成度/秒
    { double gravity[3] = {0, -1, 0}, user[3] = {0.25, 0, 0.5}, rotation[3] = {1, -2, 0.5}; s2a_gc_motion_axes(gravity, user, rotation, a, g);
      near3("⑧ 手柄 加速度合成", a, -0.25, 1, -0.5); near3("⑧ 手柄 角速度换算", g, deg, -2 * deg, 0.5 * deg); }
    if (bad) { printf("✗ t_phone_axes：%d 处不符\n", bad); return 1; }
    printf("✓ t_phone_axes：8 项手算姿态全符（横屏左右、平放对齐 NS2 真报文、方向盘、俯仰、手柄 GCMotion）\n");
    return 0;
}
