#import "vpad_settings.h"
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#include <math.h>

@interface S2AVPadSettings () <UIDocumentPickerDelegate>
@end
@implementation S2AVPadSettings {
    S2AVPadLayoutStore *_store;
    S2AVPadLayout *_current;
    NSArray<S2AVPadLayout *> *_layouts;
    void (^_changed)(void), (^_closed)(void);
    NSURL *_exportDirectory;
    BOOL _importing;
}
- (instancetype)initWithStore:(S2AVPadLayoutStore *)store current:(S2AVPadLayout *)layout
                      changed:(void (^)(void))changed closed:(void (^)(void))closed {
    if (!(self = [super initWithStyle:UITableViewStyleInsetGrouped])) return nil;
    _store = store; _current = layout; _changed = [changed copy]; _closed = [closed copy];
    self.title = @"触屏布局";
    return self;
}
- (void)viewDidLoad {
    [super viewDidLoad];
    if (!self.embeddedInSettings) {
        self.navigationItem.rightBarButtonItem = [[UIBarButtonItem alloc] initWithTitle:@"完成" style:UIBarButtonItemStyleDone target:self action:@selector(close)];
        self.navigationItem.rightBarButtonItem.accessibilityIdentifier = @"vpad.layouts.done";
    }
    self.navigationController.modalInPresentation = YES;
    if (self.embeddedInSettings) {
        self.tableView.backgroundColor = UIColor.clearColor;
        [self updateMaterial];
        [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(updateMaterial) name:UIAccessibilityReduceTransparencyStatusDidChangeNotification object:nil];
    }
    [self reload];
}
- (void)dealloc { [NSNotificationCenter.defaultCenter removeObserver:self]; }
- (void)updateMaterial {
    BOOL solid = UIAccessibilityIsReduceTransparencyEnabled();
    UIVisualEffectView *material = [[UIVisualEffectView alloc] initWithEffect:solid ? nil : [UIBlurEffect effectWithStyle:UIBlurEffectStyleSystemMaterial]];
    material.backgroundColor = solid ? UIColor.systemGroupedBackgroundColor : UIColor.clearColor;
    self.tableView.backgroundView = material; [self.tableView reloadData];
}
- (void)reload { _layouts = [_store layouts]; [self.tableView reloadData]; }
- (void)close { [self dismissViewControllerAnimated:YES completion:^{ if (self->_closed) self->_closed(); }]; }
- (void)showError:(NSError *)error {
    UIAlertController *alert = [UIAlertController alertControllerWithTitle:@"布局操作失败" message:error.localizedDescription preferredStyle:UIAlertControllerStyleAlert];
    [alert addAction:[UIAlertAction actionWithTitle:@"知道了" style:UIAlertActionStyleCancel handler:nil]];
    UIViewController *presented = self.presentedViewController;
    if (presented) [presented dismissViewControllerAnimated:YES completion:^{ [self presentViewController:alert animated:YES completion:nil]; }];
    else [self presentViewController:alert animated:YES completion:nil];
}
- (NSInteger)numberOfSectionsInTableView:(UITableView *)tableView { return _store.diagnostics.count ? 5 : 4; }
- (NSInteger)tableView:(UITableView *)tableView numberOfRowsInSection:(NSInteger)section {
    return section == 0 ? _layouts.count + 2 : section == 1 ? 4 : section == 2 ? 3 : section == 3 ? 3 : _store.diagnostics.count;
}
// 0 = 默认布局，1 = 触屏直通，之后是用户布局（内置两项没有对应文件，所以固定占前两行）
- (NSString *)identifierAtRow:(NSInteger)row {
    if (row == 0) return S2AVPadLayoutDefaultIdentifier;
    if (row == 1) return S2AVPadLayoutTouchIdentifier;
    return _layouts[row - 2].identifier;
}
- (NSString *)tableView:(UITableView *)tableView titleForHeaderInSection:(NSInteger)section {
    return section == 0 ? @"选择布局" : section == 1 ? @"管理布局" : section == 2 ? @"按键显示与触感" : section == 3 ? @"摇杆手感" : @"未加载的文件";
}
- (NSString *)tableView:(UITableView *)tableView titleForFooterInSection:(NSInteger)section {
    return section == 0 ? @"切换立即保存并生效" :
        section == 1 ? nil : section == 2 ? @"停止操作 3 秒后保留的按键透明度。0% 会完全隐藏；默认 25%。触感反馈在按键新按下、每根摇杆每次按住期间首次到边时轻震一次；不改变游戏本身的震动。" :
        section == 3 ? @"左右摇杆共用。死区过滤中心微动；外圈饱和越小，越早推满；响应指数大于 1 时中心更细，小于 1 时更灵敏。松手保存，下次推杆生效；默认 8% / 100% / 1.00。" :
        @"错误文件保留原样；修正后重新导入或从“文件”中的本 App / VPadLayouts 管理。";
}
- (UITableViewCell *)tableView:(UITableView *)tableView cellForRowAtIndexPath:(NSIndexPath *)indexPath {
    UITableViewCell *cell = [[UITableViewCell alloc] initWithStyle:UITableViewCellStyleSubtitle reuseIdentifier:nil];
    if (indexPath.section == 0) {
        NSString *identifier = [self identifierAtRow:indexPath.row];
        S2AVPadLayout *layout = indexPath.row >= 2 ? _layouts[indexPath.row - 2] : nil;
        cell.textLabel.text = layout ? layout.name : indexPath.row == 0 ? @"按键布局" : @"仅触屏操作";
        cell.detailTextLabel.text = layout ? layout.identifier : indexPath.row == 0 ?
            @"显示虚拟按键" : @"游戏自己的触屏操作用这项；虚拟按键会关掉，四指长按 1.5 秒可切回";   // 审计 P1-5：原文「推荐连接手柄时使用」把该选的场景说反了
        cell.accessoryType = [_store.selectedIdentifier isEqualToString:identifier] ? UITableViewCellAccessoryCheckmark : UITableViewCellAccessoryNone;
        cell.accessibilityIdentifier = [@"vpad.layout." stringByAppendingString:identifier];
    } else if (indexPath.section == 1) {
        cell.textLabel.text = @[@"导入布局", @"另存当前布局", @"导出当前布局", @"恢复按键布局"][indexPath.row];
        cell.accessibilityIdentifier = @[@"vpad.layouts.import", @"vpad.layouts.save", @"vpad.layouts.export", @"vpad.layouts.reset"][indexPath.row];
        cell.textLabel.textColor = self.view.tintColor;
    } else if (indexPath.section == 2 && indexPath.row == 2) {
        cell.textLabel.text = @"按住锁定 ZL / ZR"; cell.detailTextLabel.text = @"按住 2 秒锁定，再点一下解锁；只对这款游戏生效";
        cell.selectionStyle = UITableViewCellSelectionStyleNone;
        UISwitch *toggle = [[UISwitch alloc] init]; toggle.on = _store.holdLockEnabled;
        toggle.accessibilityIdentifier = @"vpad.holdLock.enabled"; toggle.accessibilityLabel = @"按住锁定 ZL / ZR";
        [toggle addTarget:self action:@selector(holdLockChanged:) forControlEvents:UIControlEventValueChanged];
        cell.accessoryView = toggle;
    } else if (indexPath.section == 2 && indexPath.row == 1) {
        cell.textLabel.text = @"触感反馈"; cell.selectionStyle = UITableViewCellSelectionStyleNone;
        UISwitch *toggle = [[UISwitch alloc] init]; toggle.on = _store.hapticFeedbackEnabled;
        toggle.accessibilityIdentifier = @"vpad.haptics.enabled"; toggle.accessibilityLabel = @"触感反馈";
        [toggle addTarget:self action:@selector(hapticsChanged:) forControlEvents:UIControlEventValueChanged];
        cell.accessoryView = toggle;
    } else if (indexPath.section == 2) {
        cell.textLabel.text = @"闲置透明度";
        cell.detailTextLabel.text = [NSString stringWithFormat:@"%.0f%%", _store.idleKeyOpacity * 100];
        cell.selectionStyle = UITableViewCellSelectionStyleNone;
        UISlider *slider = [[UISlider alloc] initWithFrame:CGRectMake(0, 0, 150, 44)];
        slider.minimumValue = 0; slider.maximumValue = 1; slider.value = _store.idleKeyOpacity;
        slider.continuous = NO;   // 手指松开再原子保存一次，不在拖动时反复写盘。
        slider.accessibilityIdentifier = @"vpad.visibility.idleOpacity";
        slider.accessibilityLabel = @"闲置按键透明度";
        slider.accessibilityValue = cell.detailTextLabel.text;
        [slider addTarget:self action:@selector(opacityChanged:) forControlEvents:UIControlEventValueChanged];
        cell.accessoryView = slider;
    } else if (indexPath.section == 3) {
        NSInteger row = indexPath.row;
        CGFloat values[] = {_store.stickDeadzone, _store.stickSaturation, _store.stickExponent};
        float mins[] = {0, .5, .5}, maxs[] = {.4, 1, 3};
        cell.textLabel.text = @[@"径向死区", @"外圈饱和", @"响应指数"][row];
        cell.detailTextLabel.text = row == 2 ? [NSString stringWithFormat:@"%.2f", values[row]] : [NSString stringWithFormat:@"%.0f%%", values[row] * 100];
        UISlider *slider = [[UISlider alloc] initWithFrame:CGRectMake(0, 0, 150, 44)];
        slider.minimumValue = mins[row]; slider.maximumValue = maxs[row]; slider.value = values[row]; slider.tag = row; slider.continuous = NO;
        slider.accessibilityLabel = cell.textLabel.text; slider.accessibilityValue = cell.detailTextLabel.text;
        slider.accessibilityIdentifier = @[@"vpad.stick.deadzone", @"vpad.stick.saturation", @"vpad.stick.exponent"][row];
        [slider addTarget:self action:@selector(stickChanged:) forControlEvents:UIControlEventValueChanged];
        cell.accessoryView = slider; cell.selectionStyle = UITableViewCellSelectionStyleNone;
    } else {
        cell.textLabel.text = _store.diagnostics[indexPath.row]; cell.textLabel.numberOfLines = 0;
        cell.selectionStyle = UITableViewCellSelectionStyleNone;
    }
    cell.textLabel.font = [UIFont preferredFontForTextStyle:UIFontTextStyleBody];
    cell.detailTextLabel.font = [UIFont preferredFontForTextStyle:UIFontTextStyleFootnote];
    cell.textLabel.adjustsFontForContentSizeCategory = cell.detailTextLabel.adjustsFontForContentSizeCategory = YES;
    cell.textLabel.numberOfLines = cell.detailTextLabel.numberOfLines = 0;
    if (self.embeddedInSettings) cell.backgroundColor = [UIColor.secondarySystemGroupedBackgroundColor colorWithAlphaComponent:UIAccessibilityIsReduceTransparencyEnabled() ? 1 : 0.75];
    return cell;
}
- (void)hapticsChanged:(UISwitch *)toggle {
    NSError *error;
    if (![_store setHapticFeedbackEnabled:toggle.on error:&error]) { toggle.on = _store.hapticFeedbackEnabled; [self showError:error]; return; }
    // Feedback is independent of manual layout selection / touch mode.
    [self reload];
}
- (void)holdLockChanged:(UISwitch *)toggle {
    NSError *error;
    if (![_store setHoldLockEnabled:toggle.on error:&error]) { toggle.on = _store.holdLockEnabled; [self showError:error]; return; }
    [self reload];
}
- (void)opacityChanged:(UISlider *)slider {
    NSError *error;
    CGFloat value = roundf(slider.value * 100) / 100;
    if (![_store setIdleKeyOpacity:value error:&error]) { slider.value = _store.idleKeyOpacity; [self showError:error]; return; }
    // Appearance alone must not mark a manual layout choice or change touch mode.
    [self reload];
}
- (void)stickChanged:(UISlider *)slider {
    CGFloat values[] = {_store.stickDeadzone, _store.stickSaturation, _store.stickExponent};
    values[slider.tag] = round(slider.value * 100) / 100;
    NSError *error;
    if (![_store setStickDeadzone:values[0] saturation:values[1] exponent:values[2] error:&error]) [self showError:error];
    // 不调用布局选择回调，避免手感设置把“仅触屏”或自动选择状态切成按键模式。
    [self reload];
}
- (BOOL)chooseLayout:(NSString *)identifier {
    NSError *error;
    if (![_store selectIdentifier:identifier error:&error]) { [self showError:error]; return NO; }
    _changed();
    if (self.currentLayout) _current = self.currentLayout();
    else if (![S2AVPadLayout isBuiltinIdentifier:identifier]) _current = [_store selectedLayout:&error];
    // The settings page stays open while the virtual pad regenerates its keys.
    [self reload]; return YES;
}
- (void)tableView:(UITableView *)tableView didSelectRowAtIndexPath:(NSIndexPath *)indexPath {
    [tableView deselectRowAtIndexPath:indexPath animated:YES];
    if (indexPath.section == 0) {
        if ([self chooseLayout:[self identifierAtRow:indexPath.row]] && !self.embeddedInSettings) [self close];
    } else if (indexPath.section == 1) {
        switch (indexPath.row) {
            case 0: [self importLayout]; break;
            case 1: [self saveCurrent]; break;
            case 2: [self exportCurrent]; break;
            case 3: [self confirmReset]; break;
        }
    }
}
- (void)confirmReset {
    UIAlertController *alert = [UIAlertController alertControllerWithTitle:@"恢复按键布局？" message:@"已保存的自定义布局会保留。" preferredStyle:UIAlertControllerStyleAlert];
    [alert addAction:[UIAlertAction actionWithTitle:@"取消" style:UIAlertActionStyleCancel handler:nil]];
    [alert addAction:[UIAlertAction actionWithTitle:@"恢复按键布局" style:UIAlertActionStyleDefault handler:^(UIAlertAction *action) {
        if ([self chooseLayout:S2AVPadLayoutDefaultIdentifier] && !self.embeddedInSettings) [self close];
    }]];
    [self presentViewController:alert animated:YES completion:nil];
}
- (void)saveCurrent {
    UIAlertController *alert = [UIAlertController alertControllerWithTitle:@"另存当前布局" message:@"为当前布局命名，保存为一个新方案。" preferredStyle:UIAlertControllerStyleAlert];
    [alert addTextFieldWithConfigurationHandler:^(UITextField *field) { field.placeholder = @"布局名称"; field.text = [self->_current.name stringByAppendingString:@" 副本"]; field.accessibilityIdentifier = @"vpad.layouts.name"; }];
    [alert addAction:[UIAlertAction actionWithTitle:@"取消" style:UIAlertActionStyleCancel handler:nil]];
    [alert addAction:[UIAlertAction actionWithTitle:@"保存布局" style:UIAlertActionStyleDefault handler:^(UIAlertAction *action) {
        NSMutableDictionary *doc = [self->_current.document mutableCopy]; doc[@"id"] = NSUUID.UUID.UUIDString.lowercaseString;
        doc[@"name"] = [alert.textFields.firstObject.text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        NSError *error; NSData *data = [NSJSONSerialization dataWithJSONObject:doc options:0 error:&error];
        S2AVPadLayout *layout = data ? [S2AVPadLayout readData:data error:&error] : nil;
        if (!layout || ![self->_store saveLayout:layout error:&error]) { [self showError:error]; return; }
        if ([self chooseLayout:layout.identifier]) self->_current = layout;
    }]];
    [self presentViewController:alert animated:YES completion:nil];
}
- (void)importLayout {
    _importing = YES;
    UIDocumentPickerViewController *picker = [[UIDocumentPickerViewController alloc] initForOpeningContentTypes:@[UTTypeJSON] asCopy:YES];
    picker.delegate = self; picker.allowsMultipleSelection = NO;
    [self presentViewController:picker animated:YES completion:nil];
}
- (void)cleanExport {
    if (_exportDirectory) [NSFileManager.defaultManager removeItemAtURL:_exportDirectory error:nil];
    _exportDirectory = nil;
}
- (void)exportCurrent {
    [self cleanExport]; _importing = NO;
    _exportDirectory = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:[@"s2arm-layout-" stringByAppendingString:NSUUID.UUID.UUIDString]] isDirectory:YES];
    NSError *error;
    if (![NSFileManager.defaultManager createDirectoryAtURL:_exportDirectory withIntermediateDirectories:NO attributes:nil error:&error]) { [self showError:error]; return; }
    NSMutableDictionary *document = [_current.document mutableCopy];
    // A built-in export becomes an importable user layout, without claiming the reserved default id.
    if ([S2AVPadLayout isBuiltinIdentifier:_current.identifier]) document[@"id"] = [@"layout-" stringByAppendingString:NSUUID.UUID.UUIDString.lowercaseString];
    NSURL *url = [_exportDirectory URLByAppendingPathComponent:[document[@"id"] stringByAppendingString:@".json"]];
    NSData *data = [NSJSONSerialization dataWithJSONObject:document options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:&error];
    if (!data || ![data writeToURL:url options:NSDataWritingAtomic error:&error]) { [self cleanExport]; [self showError:error]; return; }
    UIDocumentPickerViewController *picker = [[UIDocumentPickerViewController alloc] initForExportingURLs:@[url] asCopy:YES];
    picker.delegate = self; [self presentViewController:picker animated:YES completion:nil];
}
- (void)documentPicker:(UIDocumentPickerViewController *)controller didPickDocumentsAtURLs:(NSArray<NSURL *> *)urls {
    if (!_importing) { [self cleanExport]; return; }
    NSURL *url = urls.firstObject; if (!url) return;
    BOOL scoped = [url startAccessingSecurityScopedResource];
    NSError *error; NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:url.path error:&error];
    NSData *data;
    if ([attributes[NSFileType] isEqualToString:NSFileTypeRegular] && [attributes[NSFileSize] unsignedLongLongValue] <= 65536)
        data = [NSData dataWithContentsOfURL:url options:0 error:&error];
    else if (attributes) error = [NSError errorWithDomain:@"S2Arm.VPadLayout" code:1 userInfo:@{NSLocalizedDescriptionKey: @"F01.file：请选择 64 KiB 以内的普通 JSON 文件"}];
    if (scoped) [url stopAccessingSecurityScopedResource];
    S2AVPadLayout *layout = data ? [S2AVPadLayout readData:data error:&error] : nil;
    if (!layout || ![_store saveLayout:layout error:&error]) { [self showError:error]; return; }
    if ([self chooseLayout:layout.identifier]) _current = layout;
}
- (void)documentPickerWasCancelled:(UIDocumentPickerViewController *)controller { [self cleanExport]; }
@end
