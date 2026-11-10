#pragma once
#import <UIKit/UIKit.h>

// The game host supplies real runtime capabilities. The input workbench has no
// game session and leaves these absent.
@interface S2ASettingsRuntime : NSObject
// Optional host action; shells without a game leave this absent.
@property(nonatomic, copy) void (^manageSaves)(UIViewController *presenter);
@property(nonatomic, copy) BOOL (^canRestart)(void);
// Main thread. Complete once on the main thread; nil error means the new guest
// thread started, not that its first frame or save data has been verified.
@property(nonatomic, copy) void (^restart)(void (^completion)(NSString *error));
// 切换游戏：只有多款容器（一个 App 装多款游戏，runtime/src/launchctx.c）给，单款包为 nil。一个进程只跑一款：
//   弹确认，确认后照退出流程存档收尾、结束进程，再点开 App 是游戏库。设置页「切换游戏」一栏与齿轮菜单都调它；主线程
@property(nonatomic, copy) void (^switchGame)(UIViewController *presenter);
@end

// Main thread only. A shell without a game may leave runtime capabilities nil.
void s2a_settings_set_runtime(S2ASettingsRuntime *runtime);
// 游戏内齿轮菜单（ios/vpad.m）用：存档管理 / 重启 / 是否多款容器；没登记时 nil
S2ASettingsRuntime *s2a_settings_runtime(void);
// Main thread only, after the visible shell presenter and its ordinary input /
// diagnostic attachments are ready. Works before a game starts and during play;
// missing game capabilities are disabled. A nil or already-presenting controller
// is ignored, so callers should deliver pending actions after their modal closes.
// Input stays isolated through nested layout/file/share/confirmation flows until
// the whole settings navigation closes. This entry never starts a game.
void s2a_settings_present(UIViewController *presenter);
