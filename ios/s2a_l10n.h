// 宿主界面文字的本地化（#316）：字符串表在 App 主 bundle 的 <语言>.lproj/S2ArmHost.strings（源 ios/l10n/，runtime/build.mk 的 ios 目标拷进包）。
//   不用游戏库的 Localizable.xcstrings：单款包不带游戏库资源包。键是稳定的点分 id（按文件 / 功能分前缀），同一句话复用同一个键；
//   含格式的串写 [NSString stringWithFormat:S2AL(@"…"), …]。两表键集合、占位符一致与代码引用由 tests/ios_l10n_test.sh 核对
#pragma once
#import <Foundation/Foundation.h>

#define S2AL(key) [NSBundle.mainBundle localizedStringForKey:(key) value:nil table:@"S2ArmHost"]
