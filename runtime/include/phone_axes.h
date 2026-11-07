#ifndef S2A_PHONE_AXES_H
#define S2A_PHONE_AXES_H
// 手机机身传感器 → NS2 原始坐标（见 runtime/src/phone_axes.c）
void s2a_phone_axes(int landscape_right, const double acc_dev[3], const double gyr_dev[3], float acc_raw[3], float gyr_raw[3]);

// 手柄 GCMotion → NS2 原始坐标（Mac，macos/front_mac.m）：手柄坐标轴与 NS2 相同（x 右、y 上沿、z 正面法线），
//   重力方向取反成支撑反力，角速度 rad/s 换成度/秒。纯函数，单测 t_phone_axes
static inline void s2a_gc_motion_axes(const double gravity[3], const double user[3], const double rotation[3], float acc_raw[3], float gyr_raw[3]) {
    for (int i = 0; i < 3; i++) {
        acc_raw[i] = (float)-(gravity[i] + user[i]);
        gyr_raw[i] = (float)(rotation[i] * 57.29577951308232);
    }
}
#endif
