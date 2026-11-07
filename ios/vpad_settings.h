#import <UIKit/UIKit.h>
#import "vpad_layout.h"

@interface S2AVPadSettings : UITableViewController
@property(nonatomic) BOOL embeddedInSettings;
@property(nonatomic, copy) void (^stickModeChanged)(void);   // 摇杆模式改了，当场生效
@property(nonatomic, copy) void (^editRequested)(void);   // 收起设置后进入按键编辑（游戏不退出）
@property(nonatomic, copy) S2AVPadLayout *(^currentLayout)(void);
- (instancetype)initWithStore:(S2AVPadLayoutStore *)store current:(S2AVPadLayout *)layout
                      changed:(void (^)(void))changed closed:(void (^)(void))closed;
@end
